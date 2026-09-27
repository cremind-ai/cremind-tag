/*
 * Glue between the Zephyr services (Bluetooth, NVS, UICR, ADC, panel) and the
 * protocol core. Everything below runs on the system work queue.
 */
#ifndef TAG_APP_H_
#define TAG_APP_H_

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/devicetree.h>

#include <ctag/proto_msgs.h>

#include "tag_core.h"

#define TAG_BOARD_NODE DT_CHOSEN(cremind_tag_board)
#define TAG_PANEL_NODE DT_CHOSEN(cremind_panel)
#define TAG_BOARD_ID   DT_PROP(TAG_BOARD_NODE, board_id)
#define TAG_PANEL_ID   DT_PROP(TAG_PANEL_NODE, panel_id)
#define TAG_WIDTH      DT_PROP(TAG_PANEL_NODE, width)
#define TAG_HEIGHT     DT_PROP(TAG_PANEL_NODE, height)
#define TAG_PLANES     DT_PROP(TAG_PANEL_NODE, planes)
#define TAG_PLANE_FLAGS DT_PROP(TAG_PANEL_NODE, plane_flags)
/* CMD{SLEEP} (System OFF) only with a physically verified wake input. */
#define TAG_SLEEP_OK   (DT_PROP(TAG_BOARD_NODE, wake_verified) && \
			DT_NODE_HAS_PROP(TAG_BOARD_NODE, wake_gpios))

extern struct tag_core app_core;

/* main.c: run the panel poll, tag_core_poll() and the GATT transmit pump
 * (now, after ms, or after a short retry delay). */
void app_kick(void);
void app_kick_after(uint32_t ms);
void app_kick_later(void);
/* main.c: progress on the link; restarts the session timeout. */
void app_touch(void);
/* main.c: the link timer expired while connected (session timeout/linger). */
void app_session_timer(void);
/* main.c: link events from gatt.c. */
void app_link_up(void);
void app_link_down(void);
/* main.c: the panel's refresh finished (panel driver work item). */
void app_refresh_done(uint8_t status);

/* gatt.c */
void gatt_pump(void);
void gatt_disconnect(void);
bool gatt_connected(void);

/* adv.c: one timer for the wake cycle (not connected) and the session
 * timeout (connected); they are never needed at the same time. */
void adv_start(void);
void adv_link_up(void);
void adv_link_down(void);
void adv_touch(uint32_t ms);

/* store_nvs.c */
int store_init(void);

/* enroll_uicr.c: the verified blob (secret wiped by the caller). */
int enroll_load(struct ctag_enrollment *e);
const uint8_t *enroll_secret(void);

/* battery.c */
int battery_init(void);

#endif /* TAG_APP_H_ */
