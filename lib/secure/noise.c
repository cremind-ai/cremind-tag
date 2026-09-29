/*
 * Noise_IK_25519_ChaChaPoly_SHA256 through the vendored Noise* API
 * (lib/third_party/noise_ik): one device object per static key and prologue,
 * one session at a time, transport messages sealed and opened through
 * Noise_IK_session_write/read. Mirrors Cremind's app/tags/runtime/secure/noise.py.
 *
 * Noise*'s IK instantiation accepts only initiators registered as peers of
 * the device ("we don't accept unknown remote static keys"), while a v2
 * device must accept any controller (grants authorise, not the handshake).
 * Before the verified state machine reads message 1, the responder therefore
 * recovers the initiator's static key from it with Noise*'s own exported
 * primitives (MixHash, MixKey(DH(s, re)), DecryptAndHash - the first half of
 * the responder's message-1 processing, on scratch state that is wiped), adds
 * it as the only peer, and then lets Noise_IK_session_read authenticate the
 * whole message. The generated code is used unmodified; the cost is one
 * extra X25519.
 *
 * Every entry into Noise* runs under the heap guard (secure_int.h): a failed
 * allocation or missing randomness returns -ENOMEM / -EIO, and every Noise
 * object is gone (the heap was reset).
 */
#include <errno.h>
#include <string.h>

#include "ctag_krml.h"

#include "IK.h"

#include <ctag/ctag_secure.h>

#include "secure_int.h"

/*
 * Confidentiality level requested for our transport messages. Before the
 * responder has received its first transport message Noise* grants level 4
 * (weak forward secrecy), afterwards 5; requesting 5 would refuse the
 * device's first answers or events. 4 is the most the pattern provides at
 * that point.
 */
#define CONF_LEVEL 4u

static const uint8_t no_sk[32]; /* device-secret serialisation key: never used */
static struct ctag_noise *guard_nz;

static bool alive(const struct ctag_noise *nz)
{
	return nz->epoch == ctag_krml_epoch();
}

/* The heap was reset: every pointer is stale. */
static void forget(struct ctag_noise *nz)
{
	nz->dv = NULL;
	nz->sn = NULL;
	nz->pt = NULL;
	nz->pt_len = 0u;
	nz->pid = 0u;
	nz->prologue_len = 0u;
}

static void check_alive(struct ctag_noise *nz)
{
	if (!alive(nz)) {
		forget(nz);
		nz->epoch = ctag_krml_epoch();
	}
}

static int guard_end(int rc)
{
	ctag_krml_disarm();
	guard_nz = NULL;
	return rc;
}

static void release_pt(struct ctag_noise *nz)
{
	if (nz->pt != NULL) {
		ctag_krml_free(nz->pt); /* wiped */
	}
	nz->pt = NULL;
	nz->pt_len = 0u;
}

static void drop_session(struct ctag_noise *nz)
{
	release_pt(nz);
	if (nz->sn != NULL) {
		Noise_IK_session_free(nz->sn);
		nz->sn = NULL;
	}
	if (nz->pid != 0u && nz->dv != NULL) {
		Noise_IK_device_remove_peer(nz->dv, nz->pid);
	}
	nz->pid = 0u;
}

size_t ctag_noise_prologue(uint8_t link, const uint8_t device_id[CTAG_DEVICE_ID_LEN],
			   uint8_t out[CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN])
{
	memcpy(out, CTAG_V2_PROLOGUE, CTAG_V2_PROLOGUE_LEN);
	out[CTAG_V2_PROLOGUE_LEN] = link;
	memcpy(&out[CTAG_V2_PROLOGUE_LEN + 1u], device_id, CTAG_DEVICE_ID_LEN);
	return CTAG_V2_PROLOGUE_LEN + 1u + CTAG_DEVICE_ID_LEN;
}

/* A device object for this static key and prologue (kept across sessions). */
static int ensure_device(struct ctag_noise *nz, const uint8_t s_priv[32], const uint8_t *prologue,
			 size_t plen)
{
	if (plen == 0u || plen > sizeof(nz->prologue)) {
		return -EINVAL;
	}
	if (nz->dv != NULL) {
		uint8_t cur[32];
		bool same;

		Noise_IK_device_get_static_priv(cur, nz->dv);
		same = plen == nz->prologue_len && memcmp(prologue, nz->prologue, plen) == 0 &&
		       ctag_secure_equal(cur, s_priv, sizeof(cur));
		ctag_secure_wipe(cur, sizeof(cur));
		if (same) {
			return 0;
		}
		Noise_IK_device_free(nz->dv);
		nz->dv = NULL;
		nz->prologue_len = 0u;
	}
	nz->dv = Noise_IK_device_create((uint32_t)plen, ctag_mut(prologue), NULL, ctag_mut(no_sk),
					ctag_mut(s_priv));
	if (nz->dv == NULL) {
		return -EINVAL;
	}
	memcpy(nz->prologue, prologue, plen);
	nz->prologue_len = (uint8_t)plen;
	return 0;
}

/*
 * The initiator's static key from message 1: h = protocol name, ck = h,
 * MixHash(prologue), MixHash(s_pub), MixHash(re), MixKey(DH(s, re)),
 * DecryptAndHash(enc_s) - exactly the responder's steps up to "s".
 */
static int peek_initiator(const uint8_t s_priv[32], const uint8_t *prologue, size_t plen,
			  const uint8_t *msg1, uint8_t rs[32])
{
	uint8_t h[32], ck[32], k[32], s_pub[32], re[32], enc[48];
	Noise_IK_error_code err;

	memcpy(h, CTAG_V2_NOISE_PROTOCOL, sizeof(h)); /* exactly HASHLEN bytes */
	memcpy(ck, h, sizeof(ck));
	memset(k, 0, sizeof(k));
	Noise_IK_mix_hash(h, (uint32_t)plen, ctag_mut(prologue));
	ctag_secure_x25519_public(s_priv, s_pub);
	Noise_IK_mix_hash(h, sizeof(s_pub), s_pub);
	memcpy(re, msg1, sizeof(re));
	Noise_IK_mix_hash(h, sizeof(re), re);
	err = Noise_IK_mix_dh(ctag_mut(s_priv), re, k, ck, h);
	if (err == Noise_IK_CSuccess) {
		memcpy(enc, &msg1[32], sizeof(enc));
		err = Noise_IK_decrypt_and_hash(32u, rs, enc, k, h, 0u);
	}
	ctag_secure_wipe(h, sizeof(h));
	ctag_secure_wipe(ck, sizeof(ck));
	ctag_secure_wipe(k, sizeof(k));
	return err == Noise_IK_CSuccess ? 0 : -EBADMSG;
}

/* An empty handshake payload was received. */
static bool empty_payload(Noise_IK_encap_message_t *emp)
{
	uint32_t n = 0u;
	uint8_t *msg = NULL;
	bool ok = emp != NULL && Noise_IK_unpack_message(&n, &msg, emp) && n == 0u;

	if (msg != NULL) {
		ctag_krml_free(msg);
	}
	if (emp != NULL) {
		Noise_IK_encap_message_p_free(emp);
	}
	return ok;
}

/* Write a handshake message with an empty payload into out (exactly len bytes). */
static int write_empty(Noise_IK_session_t *sn, uint8_t *out, size_t len)
{
	Noise_IK_encap_message_t *emp = Noise_IK_pack_message(0u, NULL);
	uint32_t n = 0u;
	uint8_t *msg = NULL;
	Noise_IK_rcode rc = Noise_IK_session_write(emp, sn, &n, &msg);
	int err = 0;

	Noise_IK_encap_message_p_free(emp);
	if (!Noise_IK_rcode_is_success(rc) || n != len) {
		err = -EBADMSG;
	} else {
		memcpy(out, msg, len);
	}
	if (msg != NULL) {
		ctag_krml_free(msg);
	}
	return err;
}

static int accept_inner(struct ctag_noise *nz, const uint8_t s_priv[32], const uint8_t *prologue,
			size_t plen, const uint8_t *msg1, size_t len, uint8_t msg2[48],
			uint8_t rs[32], uint8_t h[32])
{
	Noise_IK_encap_message_t *payload = NULL;
	Noise_IK_peer_t *peer;
	int err;

	drop_session(nz);
	if (len != CTAG_SECURE_MSG1_LEN) {
		return -EBADMSG; /* message 1 carries an empty payload */
	}
	err = ensure_device(nz, s_priv, prologue, plen);
	if (err != 0) {
		return err;
	}
	if (peek_initiator(s_priv, prologue, plen, msg1, rs) != 0) {
		return -EBADMSG;
	}
	peer = Noise_IK_device_add_peer(nz->dv, NULL, rs);
	if (peer == NULL) {
		return -EBADMSG;
	}
	nz->pid = Noise_IK_peer_get_id(peer);
	nz->sn = Noise_IK_session_create_responder(nz->dv);
	if (nz->sn == NULL) {
		drop_session(nz);
		return -EBADMSG;
	}
	if (!Noise_IK_rcode_is_success(
		    Noise_IK_session_read(&payload, nz->sn, (uint32_t)len, ctag_mut(msg1))) ||
	    !empty_payload(payload) || Noise_IK_session_get_peer_id(nz->sn) != nz->pid) {
		drop_session(nz);
		return -EBADMSG;
	}
	if (write_empty(nz->sn, msg2, CTAG_SECURE_MSG2_LEN) != 0 ||
	    Noise_IK_session_get_status(nz->sn) != Noise_IK_Transport) {
		drop_session(nz);
		return -EBADMSG;
	}
	Noise_IK_session_get_hash(h, nz->sn);
	return 0;
}

int ctag_noise_accept(struct ctag_noise *nz, const uint8_t s_priv[32], const uint8_t *prologue,
		      size_t prologue_len, const uint8_t *msg1, size_t len,
		      uint8_t msg2[CTAG_SECURE_MSG2_LEN], uint8_t rs[32], uint8_t h[32])
{
	check_alive(nz);
	guard_nz = nz;
	if (setjmp(ctag_krml_env) != 0) {
		forget(guard_nz);
		guard_nz = NULL;
		return ctag_krml_reason;
	}
	ctag_krml_arm();
	return guard_end(accept_inner(nz, s_priv, prologue, prologue_len, msg1, len, msg2, rs, h));
}

static int connect_inner(struct ctag_noise *nz, const uint8_t s_priv[32], const uint8_t rs[32],
			 const uint8_t *prologue, size_t plen, uint8_t msg1[96])
{
	Noise_IK_peer_t *peer;
	int err;

	drop_session(nz);
	err = ensure_device(nz, s_priv, prologue, plen);
	if (err != 0) {
		return err;
	}
	peer = Noise_IK_device_add_peer(nz->dv, NULL, ctag_mut(rs));
	if (peer == NULL) {
		return -EINVAL;
	}
	nz->pid = Noise_IK_peer_get_id(peer);
	nz->sn = Noise_IK_session_create_initiator(nz->dv, nz->pid);
	if (nz->sn == NULL || write_empty(nz->sn, msg1, CTAG_SECURE_MSG1_LEN) != 0) {
		drop_session(nz);
		return -EINVAL;
	}
	return 0;
}

int ctag_noise_connect(struct ctag_noise *nz, const uint8_t s_priv[32], const uint8_t rs[32],
		       const uint8_t *prologue, size_t prologue_len,
		       uint8_t msg1[CTAG_SECURE_MSG1_LEN])
{
	check_alive(nz);
	guard_nz = nz;
	if (setjmp(ctag_krml_env) != 0) {
		forget(guard_nz);
		guard_nz = NULL;
		return ctag_krml_reason;
	}
	ctag_krml_arm();
	return guard_end(connect_inner(nz, s_priv, rs, prologue, prologue_len, msg1));
}

static int finish_inner(struct ctag_noise *nz, const uint8_t *msg2, size_t len, uint8_t h[32])
{
	Noise_IK_encap_message_t *payload = NULL;

	if (nz->sn == NULL) {
		return -ENOTCONN;
	}
	if (len != CTAG_SECURE_MSG2_LEN ||
	    !Noise_IK_rcode_is_success(
		    Noise_IK_session_read(&payload, nz->sn, (uint32_t)len, ctag_mut(msg2))) ||
	    !empty_payload(payload) || Noise_IK_session_get_status(nz->sn) != Noise_IK_Transport) {
		drop_session(nz);
		return -EBADMSG;
	}
	Noise_IK_session_get_hash(h, nz->sn);
	return 0;
}

int ctag_noise_finish(struct ctag_noise *nz, const uint8_t *msg2, size_t len, uint8_t h[32])
{
	check_alive(nz);
	guard_nz = nz;
	if (setjmp(ctag_krml_env) != 0) {
		forget(guard_nz);
		guard_nz = NULL;
		return ctag_krml_reason;
	}
	ctag_krml_arm();
	return guard_end(finish_inner(nz, msg2, len, h));
}

bool ctag_noise_open(const struct ctag_noise *nz)
{
	return alive(nz) && nz->sn != NULL &&
	       Noise_IK_session_get_status(nz->sn) == Noise_IK_Transport;
}

static int seal_inner(struct ctag_noise *nz, const uint8_t *pt, size_t len, uint8_t *out,
		      size_t size)
{
	Noise_IK_encap_message_t *emp;
	Noise_IK_rcode rc;
	uint32_t n = 0u;
	uint8_t *ct = NULL;
	int err;

	if (size < len + CTAG_SECURE_TAG_LEN) {
		return -EMSGSIZE;
	}
	/* The API copies the plaintext first, so out may be pt. */
	emp = Noise_IK_pack_message_with_conf_level(CONF_LEVEL, (uint32_t)len, ctag_mut(pt));
	rc = Noise_IK_session_write(emp, nz->sn, &n, &ct);
	Noise_IK_encap_message_p_free(emp);
	if (!Noise_IK_rcode_is_success(rc) || n != len + CTAG_SECURE_TAG_LEN) {
		err = -EIO; /* nonce saturated: the session is over */
		drop_session(nz);
	} else {
		memcpy(out, ct, n);
		err = (int)n;
	}
	if (ct != NULL) {
		ctag_krml_free(ct);
	}
	return err;
}

int ctag_noise_seal(struct ctag_noise *nz, const uint8_t *pt, size_t len, uint8_t *out,
		    size_t size)
{
	check_alive(nz);
	if (!ctag_noise_open(nz)) {
		return -ENOTCONN;
	}
	if (len > 0xFFFFu) {
		return -EMSGSIZE;
	}
	guard_nz = nz;
	if (setjmp(ctag_krml_env) != 0) {
		forget(guard_nz);
		guard_nz = NULL;
		return ctag_krml_reason;
	}
	ctag_krml_arm();
	return guard_end(seal_inner(nz, pt, len, out, size));
}

static int unseal_inner(struct ctag_noise *nz, const uint8_t *ct, size_t len)
{
	Noise_IK_encap_message_t *emp = NULL;
	uint32_t n = 0u;
	uint8_t *msg = NULL;
	bool ok;

	release_pt(nz);
	if (len < CTAG_SECURE_TAG_LEN) {
		drop_session(nz);
		return -EBADMSG;
	}
	ok = Noise_IK_rcode_is_success(
		Noise_IK_session_read(&emp, nz->sn, (uint32_t)len, ctag_mut(ct)));
	if (ok) {
		ok = Noise_IK_unpack_message(&n, &msg, emp);
	}
	if (emp != NULL) {
		Noise_IK_encap_message_p_free(emp);
	}
	if (!ok) {
		if (msg != NULL) {
			ctag_krml_free(msg);
		}
		drop_session(nz); /* any message that fails ends the session (3.2) */
		return -EBADMSG;
	}
	nz->pt = msg;
	nz->pt_len = n;
	return 0;
}

int ctag_noise_unseal(struct ctag_noise *nz, const uint8_t *ct, size_t len, const uint8_t **pt,
		      size_t *pt_len)
{
	int err;

	*pt = NULL;
	*pt_len = 0u;
	check_alive(nz);
	if (!ctag_noise_open(nz)) {
		return -ENOTCONN;
	}
	if (len > 0xFFFFu + CTAG_SECURE_TAG_LEN) {
		return -EMSGSIZE;
	}
	guard_nz = nz;
	if (setjmp(ctag_krml_env) != 0) {
		forget(guard_nz);
		guard_nz = NULL;
		return ctag_krml_reason;
	}
	ctag_krml_arm();
	err = guard_end(unseal_inner(nz, ct, len));
	if (err == 0) {
		*pt = nz->pt;
		*pt_len = nz->pt_len;
	}
	return err;
}

void ctag_noise_unseal_done(struct ctag_noise *nz)
{
	check_alive(nz);
	release_pt(nz);
}

void ctag_noise_close(struct ctag_noise *nz)
{
	check_alive(nz);
	guard_nz = nz;
	if (setjmp(ctag_krml_env) != 0) {
		forget(guard_nz);
		guard_nz = NULL;
		return;
	}
	ctag_krml_arm();
	drop_session(nz);
	(void)guard_end(0);
}

void ctag_noise_free(struct ctag_noise *nz)
{
	check_alive(nz);
	guard_nz = nz;
	if (setjmp(ctag_krml_env) != 0) {
		forget(guard_nz);
		guard_nz = NULL;
		return;
	}
	ctag_krml_arm();
	drop_session(nz);
	if (nz->dv != NULL) {
		Noise_IK_device_free(nz->dv);
		nz->dv = NULL;
	}
	nz->prologue_len = 0u;
	(void)guard_end(0);
}
