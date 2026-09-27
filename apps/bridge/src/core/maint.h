/*
 * Bridge maintenance port (docs/protocol.md 1.1-1.3, 1.6, 10; docs/fontpack.md
 * 4): the serial framing with credits (ctag_frame) and CBOR maps (ctag_cbor),
 * answering HELLO, PING, INFO, REBOOT, EVENT_ACK, FONT_BEGIN / FONT_DATA /
 * FONT_COMMIT / FONT_STATUS / FONT_ABORT and FLASH_TEST. Mesh and delivery
 * requests are UNSUPPORTED here. Interoperates with the companion's
 * bridge_maint/client.py (and matches its simulator, sim/device.py).
 *
 * Bytes in, bytes out: maint_rx() processes complete frames synchronously
 * (the caller's receive ring holds the frames the host may send ahead, which
 * is what HELLO's caps.credits announces).
 */
#ifndef BRIDGE_MAINT_H_
#define BRIDGE_MAINT_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/ctag_cbor.h>
#include <ctag/ctag_frame.h>

#include "fontstore.h"

#define MAINT_MAX_FRAME CONFIG_CTAG_BRIDGE_MAINT_MAX_FRAME
#define MAINT_TX_FRAME  CONFIG_CTAG_BRIDGE_MAINT_TX_FRAME
#define MAINT_CREDITS   CONFIG_CTAG_BRIDGE_MAINT_CREDITS
#define MAINT_FT_CACHE  4u
#define MAINT_COUNTERS  64u /* every bridge counter (59) fits; INFO stays below MAINT_TX_FRAME */

struct maint_io {
	void (*write)(void *ctx, const uint8_t *data, size_t len);
	/* REBOOT was answered (its bytes were passed to write). */
	void (*reboot)(void *ctx);
	/* Bridge counters for INFO; returns how many were filled. */
	size_t (*counters)(void *ctx, struct ctag_cbor_counter *items, size_t max);
};

struct maint_counters {
	uint32_t frames_rx;
	uint32_t frames_tx;
	uint32_t overruns;
	uint32_t unsupported;
	uint32_t invalid;
	uint32_t unexpected;
	uint32_t hellos;
	uint32_t credit_violations;
	uint32_t tx_held;
	uint32_t tx_dropped;
};

struct maint_flash_test {
	bool used;
	uint64_t op_id;
	uint8_t n;
	struct fontstore_test_item items[FONTSTORE_TEST_MAX];
};

struct maint {
	const struct maint_io *io;
	void *ctx;
	struct fontstore *fonts;
	const struct ctag_sha256_ops *sha;
	uint32_t boot_id;
	const char *fw;
	const char *build;
	uint8_t board;
	bool hello_done;
	struct ctag_credits cr;
	int32_t host_budget;
	struct ctag_serial_rx rx;
	size_t held; /* length of a response in tx waiting for a credit */
	struct maint_flash_test ft[MAINT_FT_CACHE];
	uint8_t ft_next;
	struct maint_counters c;
	uint8_t rxbuf[MAINT_MAX_FRAME] __aligned(4);
	/* The response being sent or held; COBS-encoded on the way out. Also
	 * FONT_COMMIT's read buffer: a held response is replaced by the next
	 * one anyway (it only waits for a credit the host has not sent). */
	uint8_t tx[MAINT_TX_FRAME] __aligned(4);
};

void maint_init(struct maint *m, const struct maint_io *io, void *ctx, struct fontstore *fonts,
		const struct ctag_sha256_ops *sha, uint32_t boot_id, const char *fw,
		const char *build, uint8_t board);

void maint_rx(struct maint *m, const uint8_t *data, size_t len);

/* Write the decoded frame in m->tx (len bytes) COBS-encoded plus 0x00. */
void maint_write_frame(struct maint *m, size_t len);

/* Frame counters of the receiver (len/crc/version errors) plus the above. */
size_t maint_counters(const struct maint *m, struct ctag_cbor_counter *items, size_t max);

#endif /* BRIDGE_MAINT_H_ */
