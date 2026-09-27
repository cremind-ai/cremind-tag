/*
 * Logical screen ("layout", docs/protocol.md 4.1-4.3): validator, command
 * iterator and the bridge's LAYOUT_BEGIN/CHUNK/COMMIT assembler (3.2-3.3).
 * Portable C99; hashing goes through a caller callback.
 */
#ifndef CTAG_LAYOUT_H_
#define CTAG_LAYOUT_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <ctag/proto_msgs.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CTAG_LAYOUT_ICON_FACE CTAG_ICON_FACE_ID

/* One command of a layout; var points into the layout bytes. */
struct ctag_layout_command {
	uint8_t op; /* enum ctag_layout_cmd */
	union {
		struct ctag_layout_cmd_clear clear;
		struct ctag_layout_cmd_glyphs glyphs;
		struct ctag_layout_cmd_icon icon;
		struct ctag_layout_cmd_line line;
		struct ctag_layout_cmd_rect rect;
		struct ctag_layout_cmd_progress progress;
		struct ctag_layout_cmd_qr qr;
	} u;
	/* GLYPHS: count entries of CTAG_LAYOUT_GLYPH_LEN bytes; QR: len text bytes. */
	const uint8_t *var;
	uint16_t offset; /* of the op byte within the layout */
};

/* Does the active font pack hold strike (face, size_px)? */
typedef bool (*ctag_layout_has_strike_fn)(void *ctx, uint16_t face, uint8_t size_px);

/*
 * Validate a layout with the checks of 4.3 in their normative order; returns
 * the status of the first failure or CTAG_STATUS_OK. The strike step (5) runs
 * only when has_strike is not NULL.
 */
uint8_t ctag_layout_validate(const uint8_t *data, size_t len, ctag_layout_has_strike_fn has_strike,
			     void *ctx);

/* Panel geometry rule of 4.4 (Rotation): CTAG_STATUS_OK or CTAG_STATUS_INVALID. */
uint8_t ctag_layout_check_panel(const struct ctag_layout_header *hdr, uint16_t native_width,
				uint16_t native_height);

struct ctag_layout_iter {
	const uint8_t *data;
	size_t len;
	size_t pos;
	uint16_t left;
};

/* Start iterating a layout (header must be present); fills *hdr. */
int ctag_layout_iter_init(struct ctag_layout_iter *it, const uint8_t *data, size_t len,
			  struct ctag_layout_header *hdr);

/* Next command; false at the end or at the first malformed command. */
bool ctag_layout_iter_next(struct ctag_layout_iter *it, struct ctag_layout_command *cmd);

/* Glyph entry i of a GLYPHS command. */
static inline void ctag_layout_glyph_at(const struct ctag_layout_command *cmd, uint8_t i,
					struct ctag_layout_glyph *g)
{
	(void)ctag_layout_glyph_unpack(g, &cmd->var[(size_t)i * CTAG_LAYOUT_GLYPH_LEN],
				       CTAG_LAYOUT_GLYPH_LEN);
}

/*
 * Incremental SHA-256 provided by the caller (the libraries stay
 * crypto-agnostic); every function returns 0 on success.
 */
struct ctag_sha256_ops {
	int (*init)(void *ctx);
	int (*update)(void *ctx, const uint8_t *data, size_t len);
	int (*finish)(void *ctx, uint8_t digest[32]);
	void *ctx;
};

/*
 * Bridge-side assembler for one layout transfer at a time (3.2 rule 4). The
 * buffer should hold CTAG_LAYOUT_HARD_MAX bytes. After a successful commit the
 * layout stays in buf (begin.total_len bytes) until the next begin.
 */
struct ctag_layout_asm {
	uint8_t *buf;
	size_t size;
	struct ctag_mesh_layout_begin begin;
	uint32_t have;     /* bit i: chunk i received */
	uint16_t last_len; /* bytes in chunk chunk_count - 1 */
	bool active;
	bool bad; /* a chunk other than the last was not full */
};

void ctag_layout_asm_init(struct ctag_layout_asm *a, uint8_t *buf, size_t size);

/* LAYOUT_BEGIN: replaces any transfer in progress. */
void ctag_layout_asm_begin(struct ctag_layout_asm *a, const struct ctag_mesh_layout_begin *b);

/*
 * LAYOUT_CHUNK: CTAG_STATUS_OK when stored; NOT_FOUND for another xfer_id,
 * INVALID for an index outside the transfer (the chunk is ignored).
 */
uint8_t ctag_layout_asm_chunk(struct ctag_layout_asm *a, const struct ctag_mesh_layout_chunk *c);

/*
 * LAYOUT_COMMIT: the first four checks of 3.3 in order: NOT_FOUND, INCOMPLETE
 * (*missing = bitmap of missing chunks), TOO_LARGE / INVALID (length), then
 * DIGEST_MISMATCH (SHA-256[0:16]; INTERNAL if hashing fails). The remaining
 * checks (assignment, revision, font pack, ctag_layout_validate) are the
 * caller's.
 */
uint8_t ctag_layout_asm_commit(struct ctag_layout_asm *a, uint16_t xfer_id, uint32_t *missing,
			       const struct ctag_sha256_ops *sha);

static inline void ctag_layout_asm_cancel(struct ctag_layout_asm *a)
{
	a->active = false;
}

#ifdef __cplusplus
}
#endif

#endif /* CTAG_LAYOUT_H_ */
