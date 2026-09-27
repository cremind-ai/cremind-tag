/*
 * Tag GATT service (protocol/spec.yaml gatt, docs/protocol.md 5): CAPS read,
 * CTRL write + indicate, DATA write without response, STATUS notify. Writes
 * go to the core's reassembler; outgoing fragments are pumped from the
 * core's queue: CTRL one indication at a time (the next after the
 * confirmation), STATUS as far as the ATT buffers allow (-ENOMEM retries after
 * a completion callback or a short delay). The queue is drained in order, so
 * AUTH_OK is confirmed before the first CREDIT goes out.
 */
#include <errno.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/kernel.h>

#include <app_version.h>
#include <ctag/proto_ids.h>

#include "app.h"

static const struct bt_uuid_128 svc_uuid = BT_UUID_INIT_128(CTAG_GATT_SERVICE_UUID_VAL);
static const struct bt_uuid_128 caps_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_CAPS_UUID_VAL);
static const struct bt_uuid_128 ctrl_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_CTRL_UUID_VAL);
static const struct bt_uuid_128 data_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_DATA_UUID_VAL);
static const struct bt_uuid_128 status_uuid = BT_UUID_INIT_128(CTAG_GATT_CHR_STATUS_UUID_VAL);

static struct bt_conn *cur;
static struct bt_gatt_indicate_params ind;
static uint8_t frag[CTAG_ATT_VALUE_MAX];
static uint8_t frag_len;
static uint8_t frag_chr;
static bool ind_busy;

/* The CAPS value: served on the characteristic and bound into the handshake
 * transcript by the core (docs/protocol.md 5.4), so both use these bytes. */
void tag_hal_caps(uint8_t out[CTAG_TAG_CAPS_LEN])
{
	const struct ctag_tag_caps caps = {
		.proto = CTAG_PROTO_VERSION,
		.tag_id = app_core.cfg.tag_id,
		.board = TAG_BOARD_ID,
		.panel = TAG_PANEL_ID,
		.width = TAG_WIDTH,
		.height = TAG_HEIGHT,
		.planes = TAG_PLANES,
		.plane_flags = TAG_PLANE_FLAGS,
		.fw_major = APP_VERSION_MAJOR,
		.fw_minor = APP_VERSION_MINOR,
		.fw_patch = APP_PATCHLEVEL,
		.max_record = CTAG_TAG_RECORD_PAYLOAD_MAX,
		.credits = TAG_CREDITS,
	};

	(void)ctag_tag_caps_pack(&caps, out, CTAG_TAG_CAPS_LEN);
}

static ssize_t read_caps(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
			 uint16_t len, uint16_t offset)
{
	uint8_t value[CTAG_TAG_CAPS_LEN];

	tag_hal_caps(value);
	return bt_gatt_attr_read(conn, attr, buf, len, offset, value, sizeof(value));
}

/* CTRL and DATA: user_data carries the enum tag_chr. */
static ssize_t write_value(struct bt_conn *conn, const struct bt_gatt_attr *attr, const void *buf,
			   uint16_t len, uint16_t offset, uint8_t flags)
{
	(void)flags;
	if (conn != cur) {
		return BT_GATT_ERR(BT_ATT_ERR_UNLIKELY);
	}
	if (offset != 0u) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
	}
	if (!tag_core_closing(&app_core)) {
		app_touch();
	}
	if (tag_core_rx(&app_core, (uint8_t)(uintptr_t)attr->user_data, buf, len)) {
		app_kick();
	}
	return len;
}

BT_GATT_SERVICE_DEFINE(tag_svc,
	BT_GATT_PRIMARY_SERVICE(&svc_uuid),
	BT_GATT_CHARACTERISTIC(&caps_uuid.uuid, BT_GATT_CHRC_READ, BT_GATT_PERM_READ,
			       read_caps, NULL, NULL),
	BT_GATT_CHARACTERISTIC(&ctrl_uuid.uuid, BT_GATT_CHRC_WRITE | BT_GATT_CHRC_INDICATE,
			       BT_GATT_PERM_WRITE, NULL, write_value, (void *)TAG_CHR_CTRL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(&data_uuid.uuid, BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_WRITE, NULL, write_value, (void *)TAG_CHR_DATA),
	BT_GATT_CHARACTERISTIC(&status_uuid.uuid, BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE,
			       NULL, NULL, NULL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* Value attributes: [4] CTRL, [9] STATUS. */
#define CTRL_ATTR   (&tag_svc.attrs[4])
#define STATUS_ATTR (&tag_svc.attrs[9])

static void indicated(struct bt_conn *conn, struct bt_gatt_indicate_params *params, uint8_t err)
{
	(void)conn;
	(void)params;
	(void)err;
	ind_busy = false;
	app_kick();
}

static void notified(struct bt_conn *conn, void *user_data)
{
	(void)conn;
	(void)user_data;
	app_kick();
}

/* Out of line: its notify parameters never sit under the core's frames. */
__attribute__((noinline)) void gatt_pump(void)
{
	int err;
	int n;

	if (cur == NULL) {
		return;
	}
	while (!ind_busy) {
		if (frag_len == 0u) {
			n = tag_core_tx_next(&app_core, &frag_chr, frag);
			if (n <= 0) {
				break;
			}
			frag_len = (uint8_t)n;
		}
		if (frag_chr == TAG_CHR_CTRL) {
			ind.attr = CTRL_ATTR;
			ind.func = indicated;
			ind.destroy = NULL;
			ind.data = frag;
			ind.len = frag_len;
			err = bt_gatt_indicate(cur, &ind); /* copies the value */
			ind_busy = err == 0;
		} else {
			struct bt_gatt_notify_params np = {
				.attr = STATUS_ATTR,
				.data = frag,
				.len = frag_len,
				.func = notified,
			};

			err = bt_gatt_notify_cb(cur, &np);
		}
		if (err == -ENOMEM) {
			app_kick_later(); /* no ATT buffer free in the system work queue */
			return;
		}
		if (err != 0) {
			/* Not subscribed or link gone: the session cannot go on. */
			gatt_disconnect();
			return;
		}
		frag_len = 0u;
	}
	if (tag_core_closing(&app_core) && frag_len == 0u && !ind_busy &&
	    tag_core_tx_empty(&app_core)) {
		gatt_disconnect();
	}
}

void gatt_disconnect(void)
{
	if (cur != NULL) {
		(void)bt_conn_disconnect(cur, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
	}
}

bool gatt_connected(void)
{
	return cur != NULL;
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err != 0u || cur != NULL) {
		return;
	}
	cur = bt_conn_ref(conn);
	frag_len = 0u;
	ind_busy = false;
	app_link_up();
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	(void)reason;
	if (conn != cur) {
		return;
	}
	bt_conn_unref(cur);
	cur = NULL;
	app_link_down();
}

BT_CONN_CB_DEFINE(tag_conn_cb) = {
	.connected = connected,
	.disconnected = disconnected,
};
