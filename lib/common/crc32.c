/*
 * CRC-32/IEEE in one of three forms (lib/Kconfig): bitwise (no table; the
 * tag checks 48 and 60 bytes), a 16-entry nibble table (font-pack indexes and
 * serial frames), or Zephyr's crc32_ieee_update(). Host builds use the table.
 */
#include <ctag/ctag_crc32.h>

#if defined(CONFIG_CTAG_CRC32_ZEPHYR)
#include <zephyr/sys/crc.h>

uint32_t ctag_crc32(uint32_t crc, const void *data, size_t len)
{
	return crc32_ieee_update(crc, data, len);
}

#elif defined(CONFIG_CTAG_CRC32_BITWISE)

uint32_t ctag_crc32(uint32_t crc, const void *data, size_t len)
{
	const uint8_t *p = data;

	crc = ~crc;
	while (len-- > 0u) {
		unsigned int k;

		crc ^= *p++;
		for (k = 0u; k < 8u; k++) {
			crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
		}
	}
	return ~crc;
}

#else

static const uint32_t crc_nibble[16] = {
	0x00000000u, 0x1DB71064u, 0x3B6E20C8u, 0x26D930ACu, 0x76DC4190u, 0x6B6B51F4u,
	0x4DB26158u, 0x5005713Cu, 0xEDB88320u, 0xF00F9344u, 0xD6D6A3E8u, 0xCB61B38Cu,
	0x9B64C2B0u, 0x86D3D2D4u, 0xA00AE278u, 0xBDBDF21Cu,
};

uint32_t ctag_crc32(uint32_t crc, const void *data, size_t len)
{
	const uint8_t *p = data;

	crc = ~crc;
	while (len-- > 0u) {
		crc ^= *p++;
		crc = (crc >> 4) ^ crc_nibble[crc & 0x0Fu];
		crc = (crc >> 4) ^ crc_nibble[crc & 0x0Fu];
	}
	return ~crc;
}

#endif
