/*
 * Protocol v2 on the gateway (docs/connect-setup.md 4-5, CONFIG_CTAG_GW_SECURE):
 *
 * - plaintext: IDENTIFY (identity, ownership, a fresh challenge),
 *   SECURE_OPEN (Noise IK message 1 -> 2; replaces any session) and
 *   SECURE_DATA (one sealed secure message); PING and HELLO as in v1;
 *   everything else answers AUTH_REQUIRED;
 * - inside the session, the access table of 4.2: an unowned gateway serves
 *   INFO, PING, STATUS, CLAIM; an owned one serves everything to the pinned
 *   controller and INFO, PING, STATUS, RECOVER to any other; the rest is
 *   NOT_OWNER;
 * - the secure endpoint (lib/secure) runs STATUS, CLAIM, RECOVER, RELEASE;
 *   the ownership record is stored (generation floor first) before the
 *   answer; RELEASE wipes the network and reboots once the answer is out;
 * - answers to requests of the session and every event are sealed into it
 *   (gw_serial.c builds them, gw_v2_seal() seals them); a message that
 *   fails to decrypt ends the session and is answered in plaintext
 *   {status: AUTH_REQUIRED}.
 */
#include <errno.h>
#include <string.h>

#include "gw_core.h"

static const char T_MALFORMED[] = "malformed CBOR payload";
static const char T_MISSING[] = "missing field";

/* ---- Endpoint glue ---- */

static int persist(void *ctx, const struct ctag_owner_record *r)
{
	struct gw_core *g = ctx;
	uint8_t raw[CTAG_OWNER_RECORD_LEN];
	int err;

	if (g->be->store_owner == NULL) {
		return -ENOSYS;
	}
	ctag_owner_record_encode(r, raw);
	err = g->be->store_owner(g->be->ctx, raw, r->gen);
	ctag_secure_wipe(raw, sizeof(raw));
	return err;
}

static int random_bytes(void *ctx, uint8_t *buf, size_t len)
{
	struct gw_core *g = ctx;

	return g->be->random != NULL ? g->be->random(g->be->ctx, buf, len) : -EIO;
}

/* "x.y.z" -> x, y, z (0 for anything else). */
static void fw_version(const char *fw, uint8_t v[3])
{
	unsigned int part = 0u, acc = 0u;

	memset(v, 0, 3u);
	for (const char *c = fw; c != NULL && *c != '\0' && part < 3u; c++) {
		if (*c >= '0' && *c <= '9') {
			acc = acc * 10u + (unsigned int)(*c - '0');
			v[part] = (uint8_t)(acc > 255u ? 255u : acc);
		} else if (*c == '.') {
			part++;
			acc = 0u;
		} else {
			break;
		}
	}
}

void gw_core_secure_init(struct gw_core *g, const uint8_t ik_priv[32], const uint8_t *rec,
			 size_t rec_len, uint32_t gen_floor)
{
	struct gw_v2 *v = &g->v2;
	struct ctag_owner_record r;
	struct ctag_secure_ops ops = {.persist = persist, .random = random_bytes, .ctx = g};
	uint8_t fw[3];

	fw_version(g->info.fw, fw);
	ctag_secure_keys_init(&v->keys, CTAG_NODE_ROLE_GATEWAY, ik_priv, NULL, g->info.board, fw[0],
			      fw[1], fw[2]);
	(void)ctag_owner_record_load(&r, rec, rec_len, gen_floor);
	ctag_secure_ep_init(&v->ep, &v->keys, &r, &ops);
	ctag_secure_wipe(&r, sizeof(r));
	v->tunnel_next = 1u;
}

bool gw_v2_privileged(const struct gw_core *g)
{
	return ctag_secure_controller_match(&g->v2.ep);
}

/* ---- Plaintext messages ---- */

/* Decode the payload's wanted fields; false = answered INVALID. */
static bool decode(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p,
		   struct ctag_cbor_field *f, size_t n, size_t required)
{
	if (ctag_cbor_decode(p, h->length, f, n) != 0) {
		g->c.invalid++;
		(void)gw_respond(g, h, CTAG_STATUS_INVALID, T_MALFORMED, false);
		return false;
	}
	for (size_t i = 0u; i < required; i++) {
		if (!f[i].present) {
			g->c.invalid++;
			(void)gw_respond(g, h, CTAG_STATUS_INVALID, T_MISSING, false);
			return false;
		}
	}
	return true;
}

static void snapshot(struct gw_core *g, struct ctag_secure_answer *a)
{
	const struct ctag_secure_ep *ep = &g->v2.ep;

	memset(a, 0, sizeof(*a));
	a->status = CTAG_STATUS_OK;
	a->owner_state = ep->rec.state;
	a->gen = ep->rec.gen;
	memcpy(a->challenge, ep->challenge, sizeof(a->challenge));
	if (ep->rec.state == CTAG_OWNER_OWNED) {
		a->fields |= CTAG_SECURE_F_OWNED;
		ctag_secure_ep_authority_id(ep, a->authority_id);
	}
}

static void identify(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct gw_resp *r;

	if (!decode(g, h, p, NULL, 0u, 0u)) {
		return;
	}
	/* 5: a fresh challenge per IDENTIFY, valid until the next IDENTIFY,
	 * grant check or reboot. */
	if (ctag_secure_draw_challenge(&g->v2.ep) != 0) {
		g->c.internal_errors++;
		(void)gw_respond(g, h, CTAG_STATUS_INTERNAL, NULL, false);
		return;
	}
	r = gw_respond(g, h, CTAG_STATUS_OK, NULL, false);
	r->v2 = GW_V2_IDENTIFY;
	snapshot(g, &r->u.ans);
}

static void secure_open(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_DATA};
	uint8_t msg2[CTAG_SECURE_MSG2_LEN];
	struct gw_resp *r;
	int err;

	if (!decode(g, h, p, &f, 1u, 1u)) {
		return;
	}
	/* Answers of the old session never go into the new one. */
	gw_serial_drop_secure(g);
	err = ctag_secure_open(&g->v2.ep, CTAG_LINK_SERIAL, f.v.str.ptr, f.v.str.len, msg2);
	if (err != 0) {
		g->v2.c.secure_failures++;
		(void)gw_respond(g, h,
				 err == -ENOMEM ? CTAG_STATUS_NO_RESOURCES
				 : err == -EIO  ? CTAG_STATUS_INTERNAL
						: CTAG_STATUS_AUTH_FAILED,
				 NULL, false);
		return;
	}
	g->v2.c.secure_opens++;
	r = gw_respond(g, h, CTAG_STATUS_OK, NULL, false);
	r->v2 = GW_V2_OPEN;
	memcpy(r->u.msg2, msg2, sizeof(msg2));
	if (gw_v2_privileged(g)) {
		gw_serial_resend_retained(g); /* 5: re-sent inside the new session */
	}
}

/* ---- Inside the session ---- */

/* The access table of connect-setup.md 4.2. */
static bool allowed(const struct gw_core *g, uint8_t type)
{
	if (gw_v2_privileged(g)) {
		return true;
	}
	switch (type) {
	case CTAG_SERIAL_MSG_INFO:
	case CTAG_SERIAL_MSG_PING:
	case CTAG_SERIAL_MSG_STATUS:
		return true;
	case CTAG_SERIAL_MSG_CLAIM:
		return g->v2.ep.rec.state != CTAG_OWNER_OWNED;
	case CTAG_SERIAL_MSG_RECOVER:
		return g->v2.ep.rec.state == CTAG_OWNER_OWNED;
	default:
		return false;
	}
}

/* RELEASE committed: nothing of the previous owner stays, and the next boot
 * starts a new network (a gateway never serves a network it does not own). */
static void released(struct gw_core *g)
{
	g->v2.c.releases++;
	gw_serial_forget(g);
	gw_tunnel_reset(g);
	gw_nodes_forget(g);
	if (g->be->release != NULL) {
		g->be->release(g->be->ctx);
	}
	gw_serial_reboot_after_answer(g);
}

static void endpoint(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct ctag_cbor_field f[3] = {
		{.key = CTAG_CBOR_KEY_GRANT},
		{.key = CTAG_CBOR_KEY_SIG},
		{.key = CTAG_CBOR_KEY_RELEASE_STAGE},
	};
	struct ctag_secure_req req = {0};
	struct ctag_secure_answer ans;
	struct gw_resp *r;
	size_t n = h->type == CTAG_SERIAL_MSG_STATUS    ? 0u
		   : h->type == CTAG_SERIAL_MSG_RELEASE ? 3u
							: 2u;

	if (!decode(g, h, p, f, n, n > 2u ? 2u : n)) {
		return;
	}
	if (n > 0u) {
		req.grant = f[0].v.str.ptr;
		req.grant_len = f[0].v.str.len;
		req.sig = f[1].v.str.ptr;
		req.sig_len = f[1].v.str.len;
	}
	if (n > 2u && f[2].present) {
		req.has_release_stage = true;
		req.release_stage = (uint32_t)f[2].v.u;
	}
	ctag_secure_handle(&g->v2.ep, h->type, &req, &ans);
	r = gw_respond(g, h, ans.status, NULL, false);
	r->v2 = GW_V2_ANSWER;
	r->u.ans = ans;
	if (ans.status != CTAG_STATUS_OK) {
		return;
	}
	switch (h->type) {
	case CTAG_SERIAL_MSG_CLAIM:
	case CTAG_SERIAL_MSG_RECOVER:
		if (h->type == CTAG_SERIAL_MSG_CLAIM) {
			g->v2.c.claims++;
		} else {
			g->v2.c.recovers++;
		}
		if (gw_v2_privileged(g)) {
			gw_serial_resend_retained(g); /* this session may have them now */
		}
		break;
	case CTAG_SERIAL_MSG_RELEASE:
		released(g);
		break;
	default:
		break;
	}
}

static void inner_request(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	switch (h->type) {
	case CTAG_SERIAL_MSG_HELLO:
	case CTAG_SERIAL_MSG_IDENTIFY:
	case CTAG_SERIAL_MSG_SECURE_OPEN:
	case CTAG_SERIAL_MSG_SECURE_DATA:
	case CTAG_SERIAL_MSG_PAIR:
	case CTAG_SERIAL_MSG_REKEY:
	case CTAG_SERIAL_MSG_MAINT_AUTH:
	case CTAG_SERIAL_MSG_RECOMMISSION:
	case CTAG_SERIAL_MSG_FACTORY_SETUP:
		/* The plaintext layer's own messages, and bridge/tag messages. */
		g->c.unsupported++;
		(void)gw_respond(g, h, CTAG_STATUS_UNSUPPORTED, NULL, false);
		return;
	default:
		break;
	}
	if (!allowed(g, h->type)) {
		g->v2.c.not_owner++;
		(void)gw_respond(g, h, CTAG_STATUS_NOT_OWNER, NULL, false);
		return;
	}
	switch (h->type) {
	case CTAG_SERIAL_MSG_STATUS:
	case CTAG_SERIAL_MSG_CLAIM:
	case CTAG_SERIAL_MSG_RECOVER:
	case CTAG_SERIAL_MSG_RELEASE:
		endpoint(g, h, p);
		break;
	default:
		gw_dispatch_inner(g, h, p); /* the v1 catalogue, DISCOVER, TUNNEL_* */
		break;
	}
}

static void secure_data(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	struct ctag_cbor_field f = {.key = CTAG_CBOR_KEY_DATA};
	struct ctag_secure_hdr ih;
	struct ctag_serial_header inner;
	const uint8_t *pt;
	size_t pt_len;

	if (!decode(g, h, p, &f, 1u, 1u)) {
		return;
	}
	if (!g->v2.ep.session) {
		g->v2.c.auth_required++;
		(void)gw_respond(g, h, CTAG_STATUS_AUTH_REQUIRED, NULL, false);
		return;
	}
	if (ctag_secure_unseal(&g->v2.ep, f.v.str.ptr, f.v.str.len, &pt, &pt_len) != 0) {
		/* 3.2: a message that fails ends the session; the answer is plaintext. */
		g->v2.c.decrypt_failures++;
		gw_serial_drop_secure(g);
		(void)gw_respond(g, h, CTAG_STATUS_AUTH_REQUIRED, NULL, false);
		return;
	}
	if (ctag_secure_hdr_unpack(&ih, pt, pt_len) != 0 ||
	    (ih.flags & (CTAG_SERIAL_FLAG_RESPONSE | CTAG_SERIAL_FLAG_EVENT)) != 0u) {
		/* Nothing to answer; the frame's credit comes back all the same. */
		g->c.unexpected_frames++;
		if (g->s.owed < UINT16_MAX) {
			g->s.owed++;
		}
		ctag_secure_unseal_done(&g->v2.ep);
		return;
	}
	inner = (struct ctag_serial_header){
		.version = CTAG_PROTO_VERSION,
		.type = ih.type,
		.request_id = ih.request_id,
		.length = (uint16_t)(pt_len - CTAG_SECURE_HEADER_LEN),
	};
	g->s.in_secure = true;
	inner_request(g, &inner, &pt[CTAG_SECURE_HEADER_LEN]);
	g->s.in_secure = false;
	ctag_secure_unseal_done(&g->v2.ep);
}

void gw_v2_outer(struct gw_core *g, const struct ctag_serial_header *h, const uint8_t *p)
{
	switch (h->type) {
	case CTAG_SERIAL_MSG_PING:
		gw_dispatch_inner(g, h, p); /* plaintext answer */
		break;
	case CTAG_SERIAL_MSG_IDENTIFY:
		identify(g, h, p);
		break;
	case CTAG_SERIAL_MSG_SECURE_OPEN:
		secure_open(g, h, p);
		break;
	case CTAG_SERIAL_MSG_SECURE_DATA:
		secure_data(g, h, p);
		break;
	default:
		/* 4.2: a v1 request in plaintext */
		g->v2.c.auth_required++;
		(void)gw_respond(g, h, CTAG_STATUS_AUTH_REQUIRED, NULL, false);
		break;
	}
}

/* ---- Answers and sealing ---- */

int gw_v2_encode(struct gw_core *g, const struct gw_resp *r, uint8_t *buf, size_t size)
{
	const struct ctag_secure_keys *k = &g->v2.keys;
	struct ctag_cbor_field f[12];
	size_t n = 0u;

	switch (r->v2) {
	case GW_V2_IDENTIFY:
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, CTAG_STATUS_OK);
		f[n++] = GW_F_TSTR(CTAG_CBOR_KEY_FW, g->info.fw, strlen(g->info.fw));
		f[n++] = GW_F_TSTR(CTAG_CBOR_KEY_BUILD, g->info.build, strlen(g->info.build));
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_PROTO, CTAG_SECURE_PROTO_VERSION);
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_ROLE, CTAG_NODE_ROLE_GATEWAY);
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_BOARD, g->info.board);
		f[n++] = GW_F_BSTR(CTAG_CBOR_KEY_DEVICE_ID, k->device_id, sizeof(k->device_id));
		f[n++] = GW_F_BSTR(CTAG_CBOR_KEY_IK, k->ik_pub, sizeof(k->ik_pub));
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_OWNER_STATE, r->u.ans.owner_state);
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_GEN, r->u.ans.gen);
		f[n++] = GW_F_BSTR(CTAG_CBOR_KEY_CHALLENGE, r->u.ans.challenge,
				   sizeof(r->u.ans.challenge));
		if ((r->u.ans.fields & CTAG_SECURE_F_OWNED) != 0u) {
			f[n++] = GW_F_BSTR(CTAG_CBOR_KEY_AUTHORITY_ID, r->u.ans.authority_id,
					   sizeof(r->u.ans.authority_id));
		}
		return ctag_cbor_encode(f, n, buf, size);
	case GW_V2_OPEN:
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, r->status);
		f[n++] = GW_F_BSTR(CTAG_CBOR_KEY_DATA, r->u.msg2, sizeof(r->u.msg2));
		return ctag_cbor_encode(f, n, buf, size);
	case GW_V2_ANSWER:
		return ctag_secure_answer_encode(&r->u.ans, buf, size);
	case GW_V2_TUNNEL:
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_STATUS, r->status);
		if (r->duplicate) {
			f[n++] = GW_F_UINT(CTAG_CBOR_KEY_DETAIL, CTAG_STATUS_DUPLICATE);
		}
		f[n++] = GW_F_UINT(CTAG_CBOR_KEY_TUNNEL, r->tunnel);
		return ctag_cbor_encode(f, n, buf, size);
	default:
		return -EINVAL;
	}
}

int gw_v2_seal(struct gw_core *g, uint8_t *out, uint8_t *buf, size_t len, size_t room)
{
	int n = ctag_secure_seal(&g->v2.ep, buf, len, buf, room);
	size_t head, prefix;

	if (n < 0) {
		if (!g->v2.ep.session) {
			gw_serial_drop_secure(g); /* the session died with it */
		}
		return n;
	}
	/* {41 (data): bstr} in canonical form, right in front of the ciphertext. */
	head = n < 24 ? 1u : n < 256 ? 2u : 3u;
	prefix = 3u + head;
	memmove(&out[prefix], buf, (size_t)n);
	out[0] = 0xA1u;
	out[1] = 0x18u;
	out[2] = (uint8_t)CTAG_CBOR_KEY_DATA;
	if (head == 1u) {
		out[3] = (uint8_t)(0x40u | (unsigned int)n);
	} else if (head == 2u) {
		out[3] = 0x58u;
		out[4] = (uint8_t)n;
	} else {
		out[3] = 0x59u;
		out[4] = (uint8_t)((unsigned int)n >> 8);
		out[5] = (uint8_t)n;
	}
	return (int)prefix + n;
}

size_t gw_v2_counters(const struct gw_core *g, struct ctag_cbor_counter *items, size_t max)
{
	const struct gw_v2_counters *c = &g->v2.c;
	struct ctag_secure_heap_stats heap;
	size_t n;

	ctag_secure_heap_stats(&heap);
	{
		const struct ctag_cbor_counter all[] = {
			CTAG_CBOR_COUNTER("secure_opens", c->secure_opens),
			CTAG_CBOR_COUNTER("secure_failures", c->secure_failures),
			CTAG_CBOR_COUNTER("auth_required", c->auth_required),
			CTAG_CBOR_COUNTER("not_owner", c->not_owner),
			CTAG_CBOR_COUNTER("decrypt_failures", c->decrypt_failures),
			CTAG_CBOR_COUNTER("claims", c->claims),
			CTAG_CBOR_COUNTER("recovers", c->recovers),
			CTAG_CBOR_COUNTER("releases", c->releases),
			CTAG_CBOR_COUNTER("tunnels", c->tunnels_opened),
			CTAG_CBOR_COUNTER("tunnel_gaps", c->tunnel_gaps),
			CTAG_CBOR_COUNTER("discovered", c->discovered),
			CTAG_CBOR_COUNTER("discovered_limited", c->discovered_limited),
			CTAG_CBOR_COUNTER("prov_security", c->prov_security),
			CTAG_CBOR_COUNTER("secure_heap_peak", (uint32_t)heap.peak),
			CTAG_CBOR_COUNTER("secure_heap_failures", heap.failures),
		};

		n = sizeof(all) / sizeof(all[0]) < max ? sizeof(all) / sizeof(all[0]) : max;
		memcpy(items, all, n * sizeof(all[0]));
	}
	return n;
}
