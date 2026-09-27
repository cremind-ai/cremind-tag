#include <ctag/ctag_utf8.h>

void ctag_utf8_feed(struct ctag_utf8 *u, const uint8_t *data, size_t len)
{
	while (len-- > 0u && !u->bad) {
		uint8_t b = *data++;

		if (u->need > 0u) {
			if (b < u->lo || b > u->hi) {
				u->bad = true;
			}
			u->need--;
			u->lo = 0x80u;
			u->hi = 0xBFu;
			continue;
		}
		if (b < 0x80u) {
			continue;
		}
		u->lo = 0x80u;
		u->hi = 0xBFu;
		if (b >= 0xC2u && b <= 0xDFu) {
			u->need = 1u;
		} else if (b >= 0xE0u && b <= 0xEFu) {
			u->need = 2u;
			if (b == 0xE0u) {
				u->lo = 0xA0u; /* no overlong 3-byte forms */
			} else if (b == 0xEDu) {
				u->hi = 0x9Fu; /* no UTF-16 surrogates */
			}
		} else if (b >= 0xF0u && b <= 0xF4u) {
			u->need = 3u;
			if (b == 0xF0u) {
				u->lo = 0x90u; /* no overlong 4-byte forms */
			} else if (b == 0xF4u) {
				u->hi = 0x8Fu; /* nothing above U+10FFFF */
			}
		} else {
			u->bad = true;
		}
	}
}
