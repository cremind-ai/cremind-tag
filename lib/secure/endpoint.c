/*
 * The device side of a v2 secure endpoint (device.py SecureDevice;
 * docs/connect-setup.md 4-5): the ownership record, single-use challenges, the
 * Noise responder and the secure messages STATUS, CLAIM, RECOVER, RELEASE
 * (the tag's two stages included), PAIR (with the setup proof), REKEY,
 * MAINT_AUTH and RECOMMISSION. Role-independent: a gateway, a bridge and a
 * tag run the same rules; what a role does with an outcome (wipe the mesh,
 * leave it, drop keys) is the application's.
 *
 * Persistence is the application's too: ops.persist() stores a new record
 * before it replaces ep->rec and before any answer that depends on it.
 */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_secure.h>

#include "secure_int.h"

void ctag_secure_ep_init(struct ctag_secure_ep *ep, const struct ctag_secure_keys *keys,
			 const struct ctag_owner_record *rec, const struct ctag_secure_ops *ops)
{
	memset(ep, 0, sizeof(*ep));
	ep->keys = keys;
	ep->ops = *ops;
	if (rec != NULL) {
		ep->rec = *rec;
	}
	ep->session_serial = 1u;
}

/* ---- Identity ---- */

int ctag_secure_draw_challenge(struct ctag_secure_ep *ep)
{
	ep->has_challenge = false;
	if (ep->ops.random == NULL ||
	    ep->ops.random(ep->ops.ctx, ep->challenge, sizeof(ep->challenge)) != 0) {
		return -EIO;
	}
	ep->has_challenge = true;
	return 0;
}

static void consume_challenge(struct ctag_secure_ep *ep)
{
	ep->has_challenge = false;
	ctag_secure_wipe(ep->challenge, sizeof(ep->challenge));
}

void ctag_secure_ep_authority_id(const struct ctag_secure_ep *ep, uint8_t out[16])
{
	if (ep->rec.state == CTAG_OWNER_OWNED) {
		ctag_secure_authority_id(ep->rec.authority_pub, out);
	} else {
		memset(out, 0, 16u);
	}
}

int ctag_secure_ident2(struct ctag_secure_ep *ep, struct ctag_ident2 *out)
{
	const struct ctag_secure_keys *k = ep->keys;

	memset(out, 0, sizeof(*out));
	if (ctag_secure_draw_challenge(ep) != 0) {
		return -EIO;
	}
	out->proto = CTAG_SECURE_PROTO_VERSION;
	out->role = k->role;
	memcpy(out->device_id, k->device_id, sizeof(out->device_id));
	memcpy(out->ik, k->ik_pub, sizeof(out->ik));
	out->owner_state = ep->rec.state;
	out->gen = ep->rec.gen;
	ctag_secure_ep_authority_id(ep, out->authority_id);
	memcpy(out->challenge, ep->challenge, sizeof(out->challenge));
	out->board = k->board;
	out->fw_major = k->fw[0];
	out->fw_minor = k->fw[1];
	out->fw_patch = k->fw[2];
	return 0;
}

/* ---- Sessions ---- */

static void end_session(struct ctag_secure_ep *ep)
{
	ep->session = false;
	ep->maint_ok = false;
	ep->session_serial++;
	ctag_secure_wipe(ep->controller, sizeof(ep->controller));
	memset(ep->h, 0, sizeof(ep->h));
}

int ctag_secure_open(struct ctag_secure_ep *ep, uint8_t link, const uint8_t *msg1, size_t len,
		     uint8_t msg2[CTAG_SECURE_MSG2_LEN])
{
	uint8_t prologue[CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN];
	size_t plen = ctag_noise_prologue(link, ep->keys->device_id, prologue);
	uint8_t rs[CTAG_IDENTITY_KEY_LEN];
	int err;

	if (ep->session) {
		end_session(ep); /* replaces any session, whatever happens next */
	}
	err = ctag_noise_accept(&ep->noise, ep->keys->ik_priv, prologue, plen, msg1, len, msg2, rs,
				ep->h);
	if (err != 0) {
		ctag_secure_wipe(rs, sizeof(rs));
		memset(ep->h, 0, sizeof(ep->h));
		return err;
	}
	memcpy(ep->controller, rs, sizeof(rs));
	ctag_secure_wipe(rs, sizeof(rs));
	ep->link = link;
	ep->maint_ok = false;
	ep->session = true;
	ep->session_serial++;
	return 0;
}

void ctag_secure_close(struct ctag_secure_ep *ep)
{
	ctag_noise_close(&ep->noise);
	if (ep->session) {
		end_session(ep);
	}
}

/* A heap reset (an aborted Noise* call anywhere) took the session with it. */
static bool session_alive(struct ctag_secure_ep *ep)
{
	if (ep->session && !ctag_noise_open(&ep->noise)) {
		end_session(ep);
	}
	return ep->session;
}

bool ctag_secure_controller_match(const struct ctag_secure_ep *ep)
{
	return ep->session && ctag_noise_open(&ep->noise) && ep->rec.state == CTAG_OWNER_OWNED &&
	       ctag_secure_equal(ep->controller, ep->rec.controller, sizeof(ep->controller));
}

int ctag_secure_seal(struct ctag_secure_ep *ep, const uint8_t *pt, size_t len, uint8_t *out,
		     size_t size)
{
	int n;

	if (!session_alive(ep)) {
		return -ENOTCONN;
	}
	n = ctag_noise_seal(&ep->noise, pt, len, out, size);
	if (n < 0 && n != -EMSGSIZE) {
		ctag_secure_close(ep);
	}
	return n;
}

int ctag_secure_unseal(struct ctag_secure_ep *ep, const uint8_t *ct, size_t len,
		       const uint8_t **pt, size_t *pt_len)
{
	int err;

	if (!session_alive(ep)) {
		*pt = NULL;
		*pt_len = 0u;
		return -ENOTCONN;
	}
	err = ctag_noise_unseal(&ep->noise, ct, len, pt, pt_len);
	if (err != 0) {
		ctag_secure_close(ep); /* 3.2: a message that fails ends the session */
	}
	return err;
}

void ctag_secure_unseal_done(struct ctag_secure_ep *ep)
{
	ctag_noise_unseal_done(&ep->noise);
}

/* ---- Secure messages ---- */

static bool commit(struct ctag_secure_ep *ep, const struct ctag_owner_record *r)
{
	if (ep->ops.persist != NULL && ep->ops.persist(ep->ops.ctx, r) != 0) {
		return false;
	}
	ep->rec = *r;
	return true;
}

/* _check(): INVALID without both fields (the challenge stays), else the rules;
 * the challenge is used up whatever the outcome. */
static uint8_t check(struct ctag_secure_ep *ep, const struct ctag_secure_req *req, uint8_t ops,
		     int8_t setup_ok, struct ctag_grant *g)
{
	const struct ctag_owner_record *r = &ep->rec;
	struct ctag_grant_ctx c = {
		.role = ep->keys->role,
		.state = r->state,
		.gen = r->gen,
		.device_id = ep->keys->device_id,
		.authority_pub = r->authority_pub,
		.owner = r->owner,
		.challenge = ep->has_challenge ? ep->challenge : NULL,
		.session_controller = ep->controller,
		.ops = ops,
		.setup_proof_ok = setup_ok,
	};
	uint8_t status;

	if (req->grant == NULL || req->sig == NULL) {
		return CTAG_STATUS_INVALID;
	}
	status = ctag_grant_check(&c, req->grant, req->grant_len, req->sig, req->sig_len, g, NULL);
	consume_challenge(ep);
	return status;
}

static void owned_record(struct ctag_owner_record *r, const struct ctag_grant *g,
			 const uint8_t *op_key)
{
	memset(r, 0, sizeof(*r));
	r->state = CTAG_OWNER_OWNED;
	r->gen = g->gen_to;
	memcpy(r->authority_pub, g->authority_pub, sizeof(r->authority_pub));
	memcpy(r->owner, g->owner, sizeof(r->owner));
	memcpy(r->controller, g->controller, sizeof(r->controller));
	if (op_key != NULL) {
		r->has_op_key = true;
		memcpy(r->op_key, op_key, sizeof(r->op_key));
	}
}

/* Commit and answer {gen}; STORAGE_ERROR when the record could not be stored. */
static void commit_gen(struct ctag_secure_ep *ep, struct ctag_owner_record *r,
		       struct ctag_secure_answer *ans)
{
	if (!commit(ep, r)) {
		ans->status = CTAG_STATUS_STORAGE_ERROR;
	} else {
		ans->status = CTAG_STATUS_OK;
		ans->fields |= CTAG_SECURE_F_GEN;
		ans->gen = ep->rec.gen;
	}
	ctag_secure_wipe(r, sizeof(*r));
}

static void do_status(struct ctag_secure_ep *ep, struct ctag_secure_answer *ans)
{
	const struct ctag_owner_record *r = &ep->rec;

	if (ctag_secure_draw_challenge(ep) != 0) {
		ans->status = CTAG_STATUS_INTERNAL;
		return;
	}
	ans->status = CTAG_STATUS_OK;
	ans->fields = CTAG_SECURE_F_STATE | CTAG_SECURE_F_GEN;
	ans->owner_state = r->state;
	ans->gen = r->gen;
	ans->controller_match = ctag_secure_controller_match(ep);
	memcpy(ans->challenge, ep->challenge, sizeof(ans->challenge));
	if (r->state == CTAG_OWNER_OWNED) {
		ans->fields |= CTAG_SECURE_F_OWNED;
		ctag_secure_authority_id(r->authority_pub, ans->authority_id);
		if (ans->controller_match) {
			/* The owner (a profile id) only to the pinned controller. */
			ans->fields |= CTAG_SECURE_F_OWNER;
			memcpy(ans->owner, r->owner, sizeof(ans->owner));
		}
		if (ep->keys->role == CTAG_NODE_ROLE_TAG && r->has_op_key) {
			ans->fields |= CTAG_SECURE_F_ROOT_PROOF;
			ctag_secure_root_proof(r->op_key, ep->h, ans->root_proof);
		}
	}
}

static void do_claim(struct ctag_secure_ep *ep, const struct ctag_secure_req *req,
		     struct ctag_secure_answer *ans)
{
	struct ctag_grant g;
	struct ctag_owner_record r;

	if (ep->keys->role != CTAG_NODE_ROLE_GATEWAY) {
		ans->status = CTAG_STATUS_UNSUPPORTED;
		return;
	}
	ans->status = check(ep, req, CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_CLAIM), -1, &g);
	if (ans->status != CTAG_STATUS_OK) {
		return;
	}
	owned_record(&r, &g, NULL);
	commit_gen(ep, &r, ans);
}

static void do_recover(struct ctag_secure_ep *ep, const struct ctag_secure_req *req,
		       struct ctag_secure_answer *ans)
{
	struct ctag_grant g;
	struct ctag_owner_record r;

	if (ep->keys->role != CTAG_NODE_ROLE_GATEWAY) {
		ans->status = CTAG_STATUS_UNSUPPORTED;
		return;
	}
	if (ep->rec.state != CTAG_OWNER_OWNED) {
		ans->status = CTAG_STATUS_NOT_OWNER;
		return;
	}
	ans->status = check(ep, req, CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_RECOVER), -1, &g);
	if (ans->status != CTAG_STATUS_OK) {
		return;
	}
	r = ep->rec;
	r.gen = g.gen_to;
	memcpy(r.controller, g.controller, sizeof(r.controller));
	commit_gen(ep, &r, ans);
}

/* The secret PAIR accepts: RELEASED -> the fresh one it showed, UNOWNED -> the label's. */
static const uint8_t *setup_secret(const struct ctag_secure_ep *ep)
{
	const struct ctag_owner_record *r = &ep->rec;

	if (r->state == CTAG_OWNER_RELEASED) {
		return r->has_override ? r->override_secret : NULL;
	}
	if (r->state == CTAG_OWNER_UNOWNED) {
		return ep->keys->has_factory_secret ? ep->keys->factory_secret : NULL;
	}
	return NULL;
}

static void do_pair(struct ctag_secure_ep *ep, const struct ctag_secure_req *req,
		    struct ctag_secure_answer *ans)
{
	uint8_t k_set[32], expected[CTAG_PROOF_LEN];
	const uint8_t *secret;
	struct ctag_grant g;
	struct ctag_owner_record r;
	bool setup_ok;

	if (ep->keys->role == CTAG_NODE_ROLE_GATEWAY) {
		ans->status = CTAG_STATUS_UNSUPPORTED;
		return;
	}
	if (ep->rec.state == CTAG_OWNER_OWNED) {
		consume_challenge(ep);
		ans->status = CTAG_STATUS_NOT_OWNER;
		return;
	}
	secret = setup_secret(ep);
	if (secret == NULL || ep->rec.locked) {
		consume_challenge(ep);
		ans->status = CTAG_STATUS_LOCKED;
		return;
	}
	if (req->op_key == NULL || req->op_key_len != CTAG_OP_KEY_LEN || req->proof == NULL) {
		consume_challenge(ep);
		ans->status = CTAG_STATUS_INVALID;
		return;
	}
	ctag_secure_k_setup(secret, ep->keys->device_id, k_set);
	ctag_secure_proof_s(k_set, ep->h, req->grant, req->grant != NULL ? req->grant_len : 0u,
			    expected);
	setup_ok = req->proof_len == CTAG_PROOF_LEN &&
		   ctag_secure_equal(expected, req->proof, CTAG_PROOF_LEN);
	ans->status = check(ep, req, CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_PAIR), setup_ok ? 1 : 0, &g);
	if (ans->status == CTAG_STATUS_PROOF_FAILED) {
		if (ep->failures < UINT8_MAX) {
			ep->failures++;
		}
	} else if (ans->status == CTAG_STATUS_OK) {
		ep->failures = 0u;
		owned_record(&r, &g, req->op_key);
		commit_gen(ep, &r, ans);
		if (ans->status == CTAG_STATUS_OK) {
			ans->fields |= CTAG_SECURE_F_PROOF;
			ctag_secure_proof_d(k_set, ep->h, req->proof, ans->proof);
		}
	}
	ctag_secure_wipe(k_set, sizeof(k_set));
	ctag_secure_wipe(expected, sizeof(expected));
}

static void do_rekey(struct ctag_secure_ep *ep, const struct ctag_secure_req *req,
		     struct ctag_secure_answer *ans)
{
	struct ctag_grant g;
	struct ctag_owner_record r;

	if (ep->keys->role == CTAG_NODE_ROLE_GATEWAY) {
		ans->status = CTAG_STATUS_UNSUPPORTED;
		return;
	}
	if (ep->rec.state != CTAG_OWNER_OWNED) {
		consume_challenge(ep);
		ans->status = CTAG_STATUS_NOT_OWNER;
		return;
	}
	if (req->op_key == NULL || req->op_key_len != CTAG_OP_KEY_LEN) {
		consume_challenge(ep);
		ans->status = CTAG_STATUS_INVALID;
		return;
	}
	ans->status = check(ep, req, CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_REKEY), -1, &g);
	if (ans->status != CTAG_STATUS_OK) {
		return;
	}
	r = ep->rec;
	r.gen = g.gen_to;
	memcpy(r.controller, g.controller, sizeof(r.controller));
	r.has_op_key = true;
	memcpy(r.op_key, req->op_key, sizeof(r.op_key));
	/* A rekey abandons a tag release in progress: its stage 1 can never follow. */
	r.has_pending_override = false;
	ctag_secure_wipe(r.pending_override, sizeof(r.pending_override));
	r.has_pending_controller = false;
	memset(r.pending_controller, 0, sizeof(r.pending_controller));
	commit_gen(ep, &r, ans);
	ans->rekeyed = ans->status == CTAG_STATUS_OK && ep->keys->role == CTAG_NODE_ROLE_TAG;
}

static bool fresh_secret(struct ctag_secure_ep *ep, uint8_t out[CTAG_SETUP_SECRET_LEN])
{
	return ep->ops.random != NULL && ep->ops.random(ep->ops.ctx, out, CTAG_SETUP_SECRET_LEN) == 0;
}

static void do_release(struct ctag_secure_ep *ep, const struct ctag_secure_req *req,
		       struct ctag_secure_answer *ans)
{
	uint32_t stage = req->has_release_stage ? req->release_stage : 1u;
	uint8_t role = ep->keys->role;
	struct ctag_grant g;
	struct ctag_owner_record r;

	if (ep->rec.state != CTAG_OWNER_OWNED) {
		consume_challenge(ep);
		ans->status = CTAG_STATUS_NOT_OWNER;
		return;
	}
	if (role == CTAG_NODE_ROLE_TAG && stage == 0u) {
		uint8_t fresh[CTAG_SETUP_SECRET_LEN];

		ans->status = check(ep, req, CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_RELEASE), -1, &g);
		if (ans->status != CTAG_STATUS_OK) {
			return;
		}
		if (!fresh_secret(ep, fresh)) {
			ans->status = CTAG_STATUS_INTERNAL;
			return;
		}
		/* Prepare: the fresh secret waits for stage 1 by the same controller. */
		r = ep->rec;
		r.has_pending_override = true;
		memcpy(r.pending_override, fresh, sizeof(fresh));
		r.has_pending_controller = true;
		memcpy(r.pending_controller, g.controller, sizeof(r.pending_controller));
		commit_gen(ep, &r, ans);
		if (ans->status == CTAG_STATUS_OK) {
			ans->fields |= CTAG_SECURE_F_DATA;
			ans->data_len = CTAG_SETUP_PAYLOAD_LEN;
			ctag_secure_setup_payload(role, ep->keys->short_id, fresh, ans->data);
		}
		ctag_secure_wipe(fresh, sizeof(fresh));
		return;
	}
	if (stage > 1u) {
		consume_challenge(ep);
		ans->status = CTAG_STATUS_INVALID;
		return;
	}
	ans->status = check(ep, req, CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_RELEASE), -1, &g);
	if (ans->status != CTAG_STATUS_OK) {
		return;
	}
	memset(&r, 0, sizeof(r));
	r.gen = g.gen_to;
	if (role == CTAG_NODE_ROLE_TAG) {
		if (!ep->rec.has_pending_override || !ep->rec.has_pending_controller ||
		    !ctag_secure_equal(ep->rec.pending_controller, g.controller,
				       sizeof(g.controller))) {
			ans->status = CTAG_STATUS_INVALID; /* stage 0 first, by the same controller */
			return;
		}
		r.state = CTAG_OWNER_RELEASED;
		r.has_override = true;
		memcpy(r.override_secret, ep->rec.pending_override, sizeof(r.override_secret));
	} else if (role == CTAG_NODE_ROLE_BRIDGE) {
		/* Removed bridges stay locked until recommissioned over local USB. */
		r.state = CTAG_OWNER_RELEASED;
		r.locked = true;
	} else {
		r.state = CTAG_OWNER_UNOWNED;
	}
	commit_gen(ep, &r, ans);
	if (ans->status == CTAG_STATUS_OK) {
		ans->fields |= CTAG_SECURE_F_DATA; /* data = b"" */
		ans->data_len = 0u;
		ans->released = true;
	}
}

static void do_maint_auth(struct ctag_secure_ep *ep, const struct ctag_secure_req *req,
			  struct ctag_secure_answer *ans)
{
	uint8_t expected[CTAG_PROOF_LEN];
	bool ok;

	if (ep->keys->role != CTAG_NODE_ROLE_BRIDGE) {
		ans->status = CTAG_STATUS_UNSUPPORTED;
		return;
	}
	if (ep->link != CTAG_LINK_SERIAL) {
		ans->status = CTAG_STATUS_NOT_OWNER; /* the maintenance port only, never over the mesh */
		return;
	}
	if (ep->rec.state != CTAG_OWNER_OWNED || !ep->rec.has_op_key) {
		ans->status = CTAG_STATUS_NOT_OWNER;
		return;
	}
	ctag_secure_maint_proof(ep->rec.op_key, ep->h, expected);
	ok = req->proof != NULL && req->proof_len == CTAG_PROOF_LEN &&
	     ctag_secure_equal(expected, req->proof, CTAG_PROOF_LEN);
	ctag_secure_wipe(expected, sizeof(expected));
	if (!ok) {
		if (ep->failures < UINT8_MAX) {
			ep->failures++;
		}
		ans->status = CTAG_STATUS_PROOF_FAILED;
		return;
	}
	ep->failures = 0u;
	ep->maint_ok = true;
	ans->status = CTAG_STATUS_OK;
}

static void do_recommission(struct ctag_secure_ep *ep, const struct ctag_secure_req *req,
			    struct ctag_secure_answer *ans)
{
	uint8_t fresh[CTAG_SETUP_SECRET_LEN];
	struct ctag_grant g;
	struct ctag_owner_record r;
	uint32_t gen;

	if (ep->keys->role != CTAG_NODE_ROLE_BRIDGE) {
		ans->status = CTAG_STATUS_UNSUPPORTED;
		return;
	}
	if (ep->link != CTAG_LINK_SERIAL) {
		ans->status = CTAG_STATUS_NOT_OWNER; /* never over the mesh */
		return;
	}
	if (ep->rec.state == CTAG_OWNER_UNOWNED) {
		/* Nothing to recommission: the label's secret stays valid. */
		consume_challenge(ep);
		ans->status = CTAG_STATUS_INVALID;
		return;
	}
	if (!fresh_secret(ep, fresh)) {
		ans->status = CTAG_STATUS_INTERNAL;
		return;
	}
	if (ep->rec.state == CTAG_OWNER_OWNED) {
		if (!ep->maint_ok) {
			ans->status = CTAG_STATUS_NOT_OWNER;
			ctag_secure_wipe(fresh, sizeof(fresh));
			return;
		}
		ans->status = check(ep, req, CTAG_GRANT_OP_BIT(CTAG_GRANT_OP_MAINT), -1, &g);
		if (ans->status != CTAG_STATUS_OK) {
			ctag_secure_wipe(fresh, sizeof(fresh));
			return;
		}
		gen = g.gen_to;
	} else {
		consume_challenge(ep); /* released (locked or not): physical presence */
		gen = ep->rec.gen;
	}
	memset(&r, 0, sizeof(r));
	r.state = CTAG_OWNER_RELEASED;
	r.gen = gen;
	r.has_override = true;
	memcpy(r.override_secret, fresh, sizeof(fresh));
	commit_gen(ep, &r, ans);
	if (ans->status == CTAG_STATUS_OK) {
		ans->fields |= CTAG_SECURE_F_DATA;
		ans->data_len = CTAG_SETUP_PAYLOAD_LEN;
		ctag_secure_setup_payload(ep->keys->role, ep->keys->short_id, fresh, ans->data);
		ans->recommissioned = true;
	}
	ctag_secure_wipe(fresh, sizeof(fresh));
}

void ctag_secure_handle(struct ctag_secure_ep *ep, uint8_t type, const struct ctag_secure_req *req,
			struct ctag_secure_answer *ans)
{
	memset(ans, 0, sizeof(*ans));
	if (!session_alive(ep)) {
		ans->status = CTAG_STATUS_AUTH_REQUIRED;
		return;
	}
	switch (type) {
	case CTAG_SERIAL_MSG_STATUS:
		do_status(ep, ans);
		break;
	case CTAG_SERIAL_MSG_CLAIM:
		do_claim(ep, req, ans);
		break;
	case CTAG_SERIAL_MSG_RECOVER:
		do_recover(ep, req, ans);
		break;
	case CTAG_SERIAL_MSG_RELEASE:
		do_release(ep, req, ans);
		break;
	case CTAG_SERIAL_MSG_PAIR:
		do_pair(ep, req, ans);
		break;
	case CTAG_SERIAL_MSG_REKEY:
		do_rekey(ep, req, ans);
		break;
	case CTAG_SERIAL_MSG_MAINT_AUTH:
		do_maint_auth(ep, req, ans);
		break;
	case CTAG_SERIAL_MSG_RECOMMISSION:
		do_recommission(ep, req, ans);
		break;
	default:
		ans->status = CTAG_STATUS_UNSUPPORTED;
		break;
	}
}

bool ctag_secure_pairing_paused(const struct ctag_secure_ep *ep)
{
	return ep->failures >= CTAG_SECURE_MAX_FAILURES;
}

int ctag_secure_ep_k_epoch(const struct ctag_secure_ep *ep, uint32_t tag_id, uint32_t epoch,
			   uint8_t out[CTAG_TAG_KEY_LEN])
{
	if (ep->rec.state != CTAG_OWNER_OWNED || !ep->rec.has_op_key) {
		return -ENOENT;
	}
	ctag_secure_k_epoch(ep->rec.op_key, tag_id, epoch, out);
	return 0;
}
