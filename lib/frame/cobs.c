/* COBS (docs/protocol.md 1.1); mirrors companion protocol/cobs.py. */
#include <errno.h>
#include <string.h>

#include <ctag/ctag_frame.h>

int ctag_cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t size)
{
	size_t start = 0u;
	size_t o = 0u;

	for (;;) {
		size_t end = start;
		size_t n;

		while (end < len && in[end] != 0u && end - start < 254u) {
			end++;
		}
		n = end - start;
		if (size - o < n + 1u) {
			return -EMSGSIZE;
		}
		out[o++] = (uint8_t)(n + 1u);
		memcpy(&out[o], &in[start], n);
		o += n;
		if (end == len) {
			return (int)o;
		}
		/* A full block implies no zero; any other block ends at a zero. */
		start = n == 254u ? end : end + 1u;
	}
}

int ctag_cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t size)
{
	size_t i = 0u;
	size_t o = 0u;

	if (len == 0u) {
		return -EINVAL;
	}
	while (i < len) {
		size_t code = in[i];
		size_t k;

		if (code == 0u || code > len - i) {
			return -EINVAL;
		}
		for (k = 1u; k < code; k++) {
			if (in[i + k] == 0u) {
				return -EINVAL;
			}
		}
		if (size - o < code - 1u) {
			return -EMSGSIZE;
		}
		memcpy(&out[o], &in[i + 1u], code - 1u);
		o += code - 1u;
		i += code;
		if (code != 0xFFu && i < len) {
			if (o == size) {
				return -EMSGSIZE;
			}
			out[o++] = 0u;
		}
	}
	return (int)o;
}

static void decoder_reset(struct ctag_cobs_decoder *d)
{
	d->len = 0u;
	d->remaining = 0u;
	d->zero_pending = false;
	d->started = false;
	d->discarding = false;
}

void ctag_cobs_decoder_init(struct ctag_cobs_decoder *d, uint8_t *buf, size_t size)
{
	d->buf = buf;
	d->size = size;
	d->errors = 0u;
	d->oversize = 0u;
	decoder_reset(d);
}

static void decoder_append(struct ctag_cobs_decoder *d, uint8_t byte)
{
	if (d->len == d->size) {
		decoder_reset(d);
		d->discarding = true;
		return;
	}
	d->buf[d->len++] = byte;
}

int ctag_cobs_decoder_put(struct ctag_cobs_decoder *d, uint8_t byte)
{
	if (byte == 0u) {
		int ret = -EAGAIN;

		if (d->discarding) {
			d->oversize++;
		} else if (d->remaining != 0u) {
			d->errors++;
		} else if (d->started) {
			ret = (int)d->len;
		}
		decoder_reset(d);
		return ret;
	}
	if (d->discarding) {
		return -EAGAIN;
	}
	if (d->remaining == 0u) {
		bool zero = d->zero_pending;

		d->started = true;
		d->remaining = (uint8_t)(byte - 1u);
		d->zero_pending = byte != 0xFFu;
		if (zero) {
			decoder_append(d, 0u);
		}
	} else {
		d->remaining--;
		decoder_append(d, byte);
	}
	return -EAGAIN;
}
