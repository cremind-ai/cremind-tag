/*
 * UltraChip UC8176 panel profile (docs/protocol.md 6, docs/tag-firmware.md
 * "Panel driver"), from the UC8176 datasheet command set (UC8176c A0.1):
 * OTP waveforms (REG_EN = 0), KW mode for one plane and KWR mode for two.
 *
 *   begin_frame     hardware reset, wait BUSY_N high, BTST, PSR, TRES, CDI,
 *                   then the data command of plane 0
 *   write_chunk     data bytes only (DC high); plane 1 first sends DTM2
 *   validate_frame  every byte of every plane staged, BUSY_N high
 *   commit_refresh  PON, wait BUSY_N high, DRF (the only refresh command;
 *                   DSP is never sent: with data_flag set it starts a refresh)
 *   wait_complete   non-blocking BUSY_N poll every 50 ms from the core work
 *                   item (panel_poll()), bounded by refresh-timeout-ms ->
 *                   REFRESH_TIMEOUT
 *   sleep           POF, wait, DSLP 0xA5 (only a hardware reset wakes it)
 *
 * Planes pass through unmodified: the CDI data-polarity bits (DDX) are
 * derived from the devicetree plane-flags so that the bytes whose SHA-256
 * the tag verified are exactly the bytes the controller receives.
 *
 * Panel id 255 (UNVERIFIED): panel_init() refuses before touching any pin or
 * the (deferred-init) SPI bus, and every other call returns -ENODEV. Panel
 * id 0 (NONE, development board without a display): no pin is touched and a
 * refresh completes after a fixed delay.
 */
#include <errno.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/kernel.h>

#include <ctag/proto_ids.h>

#include "app.h"
#include "panel.h"

#define EPD DT_CHOSEN(cremind_panel)

#define EPD_W          DT_PROP(EPD, width)
#define EPD_H          DT_PROP(EPD, height)
#define EPD_PLANES     DT_PROP(EPD, planes)
#define EPD_FLAGS      DT_PROP(EPD, plane_flags)
#define EPD_TIMEOUT_MS DT_PROP(EPD, refresh_timeout_ms)
#define EPD_PLANE_LEN  ((EPD_W / 8) * EPD_H)
#define EPD_VIRTUAL    (DT_PROP(EPD, panel_id) == CTAG_PANEL_NONE)

/* TRES: HRES[8:3] (multiple of 8), VRES[8:0]; the controller drives 400 x 300. */
BUILD_ASSERT(EPD_W % 8 == 0 && EPD_W <= 400 && EPD_H <= 300, "UC8176 geometry");

/* Commands (datasheet "COMMAND TABLE"). */
#define UC_PSR  0x00u
#define UC_POF  0x02u
#define UC_PON  0x04u
#define UC_BTST 0x06u
#define UC_DSLP 0x07u
#define UC_DTM1 0x10u
#define UC_DRF  0x12u
#define UC_DTM2 0x13u
#define UC_CDI  0x50u
#define UC_TRES 0x61u

/* PSR: RES = 00 (TRES overrides), REG_EN = 0 (OTP LUT), BWR (1 = KW), UD = 1,
 * SHL = 1 (first line G0, first byte S0: native row-major from the top-left),
 * SHD_N = 1, RST_N = 1. */
#define PSR_KW  0x1Fu
#define PSR_KWR 0x0Fu

/*
 * CDI = VBD[7:6] | DDX[5:4] | CDI[3:0] (0111 = 10 hsync, the default).
 * KW:  DDX[1] = 1 selects the "new data only" table (old data unused: every
 *      pixel takes LUTWB or LUTBW), DDX[0] = 1 means new bit 1 = white.
 * KWR: DDX[0] = 1 means B/W bit 1 = white; DDX[1] = 0 means red bit 1 = red.
 * VBD selects the white-driving LUT for the border (LUTBW / LUTW).
 */
#define FLAG_W1 ((EPD_FLAGS & 0x01) != 0) /* plane 0 bit 1 = white */
#define FLAG_R1 ((EPD_FLAGS & 0x02) != 0) /* plane 1 bit 1 = red */
#if EPD_PLANES == 1
#define EPD_PSR PSR_KW
#define EPD_DDX (0x2 | FLAG_W1)
#define EPD_VBD (FLAG_W1 ? 0x2 : 0x1)
#define EPD_DATA_CMD0 UC_DTM2 /* KW: DTM2 = "new" data */
#else
#define EPD_PSR PSR_KWR
#define EPD_DDX ((FLAG_R1 ? 0x0 : 0x2) | FLAG_W1)
#define EPD_VBD (FLAG_W1 ? 0x1 : 0x2)
#define EPD_DATA_CMD0 UC_DTM1 /* KWR: DTM1 = B/W, DTM2 = red */
#endif
#define EPD_CDI ((EPD_VBD << 6) | (EPD_DDX << 4) | 0x07)

#define RESET_MS        10
#define IDLE_WAIT_MS    500 /* reset, PON, POF: bounded blocking waits */
#define POLL_MS         50
#define VIRTUAL_REFRESH_MS 300

static const struct spi_dt_spec spi =
	SPI_DT_SPEC_GET(EPD, SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER);
static const struct gpio_dt_spec dc = GPIO_DT_SPEC_GET(EPD, dc_gpios);
static const struct gpio_dt_spec rst = GPIO_DT_SPEC_GET(EPD, reset_gpios);
static const struct gpio_dt_spec busy_pin = GPIO_DT_SPEC_GET(EPD, busy_gpios);
#if DT_NODE_HAS_PROP(EPD, bs_gpios)
static const struct gpio_dt_spec bs = GPIO_DT_SPEC_GET(EPD, bs_gpios);
#endif

static bool usable;
static bool active;  /* reset and initialised since the last deep sleep */
static bool waiting; /* a refresh is running: panel_poll() watches BUSY */
static uint8_t cur_plane;
static uint16_t staged[EPD_PLANES];
static uint32_t refresh_t0;

/* BUSY_N read as a raw level; low = busy unless busy-active-high. */
static bool busy(void)
{
	int level = gpio_pin_get_raw(busy_pin.port, busy_pin.pin);

	return DT_PROP(EPD, busy_active_high) ? level != 0 : level == 0;
}

static bool wait_idle(void)
{
	for (int i = 0; i < IDLE_WAIT_MS; i += 5) {
		if (!busy()) {
			return true;
		}
		k_msleep(5);
	}
	return !busy();
}

static int write_bytes(const uint8_t *data, size_t len)
{
	const struct spi_buf buf = {.buf = (void *)data, .len = len};
	const struct spi_buf_set set = {.buffers = &buf, .count = 1};

	return spi_write_dt(&spi, &set);
}

/* Command byte with DC low, then its parameters (in RAM for EasyDMA) with DC high. */
static int command(uint8_t cmd, const uint8_t *param, size_t len)
{
	int err;

	(void)gpio_pin_set_dt(&dc, 0);
	err = write_bytes(&cmd, 1);
	(void)gpio_pin_set_dt(&dc, 1);
	if (err == 0 && len > 0u) {
		err = write_bytes(param, len);
	}
	return err;
}

static void hw_reset(void)
{
	(void)gpio_pin_set_dt(&rst, 1);
	k_msleep(RESET_MS);
	(void)gpio_pin_set_dt(&rst, 0);
	k_msleep(RESET_MS);
}

int panel_init(uint8_t panel_id)
{
	if (panel_id == CTAG_PANEL_UNVERIFIED || panel_id != DT_PROP(EPD, panel_id)) {
		return -ENODEV; /* no pin, no bus: placeholder or mismatched panel */
	}
	if (!EPD_VIRTUAL) {
		if (!spi_is_ready_dt(&spi) || !gpio_is_ready_dt(&dc)) {
			return -ENODEV;
		}
#if DT_NODE_HAS_PROP(EPD, bs_gpios)
		/* BS low selects the 4-wire interface; set before any reset. */
		(void)gpio_pin_configure_dt(&bs, GPIO_OUTPUT_INACTIVE);
#endif
		(void)gpio_pin_configure_dt(&rst, GPIO_OUTPUT_INACTIVE);
		(void)gpio_pin_configure_dt(&dc, GPIO_OUTPUT_INACTIVE);
		(void)gpio_pin_configure_dt(&busy_pin, GPIO_DISCONNECTED);
	}
	usable = true;
	return 0;
}

int panel_begin_frame(void)
{
	uint8_t p[4];
	int err;

	if (!usable) {
		return -ENODEV;
	}
	cur_plane = 0u;
	for (int i = 0; i < EPD_PLANES; i++) {
		staged[i] = 0u;
	}
	if (EPD_VIRTUAL) {
		return 0;
	}
	active = true;
	(void)gpio_pin_configure_dt(&busy_pin, GPIO_INPUT);
	hw_reset();
	if (!wait_idle()) {
		return -EIO;
	}
	p[0] = 0x17u; /* BTST: datasheet defaults, phases A/B/C */
	p[1] = 0x17u;
	p[2] = 0x17u;
	err = command(UC_BTST, p, 3);
	p[0] = EPD_PSR;
	err = err ? err : command(UC_PSR, p, 1);
	p[0] = (uint8_t)(EPD_W >> 8);
	p[1] = (uint8_t)(EPD_W & 0xF8);
	p[2] = (uint8_t)(EPD_H >> 8);
	p[3] = (uint8_t)(EPD_H & 0xFF);
	err = err ? err : command(UC_TRES, p, 4);
	p[0] = EPD_CDI;
	err = err ? err : command(UC_CDI, p, 1);
	return err ? err : command(EPD_DATA_CMD0, NULL, 0);
}

int panel_write_plane_chunk(uint8_t plane, uint16_t offset, const uint8_t *data, size_t len)
{
	int err = 0;

	if (!usable || plane >= EPD_PLANES || plane < cur_plane || offset != staged[plane] ||
	    len > (size_t)(EPD_PLANE_LEN - offset)) {
		return -EINVAL;
	}
	if (!EPD_VIRTUAL) {
		if (plane != cur_plane) {
			err = command(UC_DTM2, NULL, 0); /* KWR: red plane */
		}
		err = err ? err : write_bytes(data, len);
	}
	cur_plane = plane;
	staged[plane] = (uint16_t)(offset + len);
	return err;
}

int panel_validate_frame(void)
{
	for (int i = 0; i < EPD_PLANES; i++) {
		if (staged[i] != EPD_PLANE_LEN) {
			return -EINVAL;
		}
	}
	/* Staging never refreshes: a busy controller here is a fault. */
	return !usable ? -ENODEV : (!EPD_VIRTUAL && busy()) ? -EIO : 0;
}

int panel_commit_refresh(void)
{
	int err;

	if (!usable) {
		return -ENODEV;
	}
	refresh_t0 = k_uptime_get_32();
	if (EPD_VIRTUAL) {
		return 0;
	}
	err = command(UC_PON, NULL, 0);
	if (err == 0 && !wait_idle()) {
		err = -EIO;
	}
	return err ? err : command(UC_DRF, NULL, 0);
}

void panel_poll(void)
{
	uint8_t status = CTAG_STATUS_OK;

	if (!waiting) {
		return;
	}
	if (EPD_VIRTUAL) {
		if (k_uptime_get_32() - refresh_t0 < VIRTUAL_REFRESH_MS) {
			app_kick_after(VIRTUAL_REFRESH_MS);
			return;
		}
	} else if (busy()) {
		if (k_uptime_get_32() - refresh_t0 < EPD_TIMEOUT_MS) {
			app_kick_after(POLL_MS);
			return;
		}
		status = CTAG_STATUS_REFRESH_TIMEOUT;
	}
	waiting = false;
	app_refresh_done(status);
}

int panel_wait_refresh_complete(void)
{
	if (!usable) {
		return -ENODEV;
	}
	waiting = true;
	app_kick_after(EPD_VIRTUAL ? VIRTUAL_REFRESH_MS : POLL_MS);
	return 0;
}

void panel_sleep(void)
{
	uint8_t check = 0xA5u; /* DSLP check code */

	if (!active) {
		return;
	}
	active = false;
	(void)command(UC_POF, NULL, 0);
	if (!wait_idle()) {
		hw_reset(); /* BUSY stuck (REFRESH_TIMEOUT): stop the controller */
	}
	(void)command(UC_DSLP, &check, 1);
	(void)gpio_pin_set_dt(&dc, 0);
	(void)gpio_pin_configure_dt(&busy_pin, GPIO_DISCONNECTED);
}

void panel_abort_frame(void)
{
	waiting = false;
	panel_sleep();
}
