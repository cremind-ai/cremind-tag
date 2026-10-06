/*
 * Solomon Systech SSD1619 panel profile (docs/protocol.md 6, docs/tag-firmware.md
 * "Panel driver"), from the SSD1619A command set and the sequence EPD-nRF5
 * (github.com/tsl0922/EPD-nRF5, EPD/SSD16xx.c) runs on the same boards: OTP
 * waveforms and the controller's internal temperature sensor.
 *
 *   init            panel supply on (en-gpios), then the SPI bus (deferred
 *                   init), BS low, RES# and DC idle
 *   begin_frame     hardware reset, SW RESET, wait BUSY low, border waveform,
 *                   internal sensor, data entry X+ then Y+, RAM window and
 *                   address counters (ram-x-offset), then WRITE RAM (B/W)
 *   write_chunk     data bytes only (DC high); plane 1 first moves the address
 *                   counters back to the window origin and sends WRITE RAM (red)
 *   validate_frame  every byte of every plane staged, BUSY low
 *   commit_refresh  DISPLAY UPDATE CONTROL 1 (RAM options), DISPLAY UPDATE
 *                   CONTROL 2 0xF7 (clock and analog on, load temperature and
 *                   LUT, display, analog and clock off), MASTER ACTIVATION
 *   wait_complete   non-blocking BUSY poll every 50 ms from the core work item
 *                   (panel_poll()), bounded by refresh-timeout-ms ->
 *                   REFRESH_TIMEOUT
 *   sleep           DEEP SLEEP mode 1 (only a hardware reset wakes it)
 *
 * Planes pass through unmodified: the DISPLAY UPDATE CONTROL 1 normal/inverse
 * options are derived from the devicetree plane-flags (native: B/W bit 1 =
 * white, red bit 1 = red) so that the bytes whose SHA-256 the tag verified are
 * exactly the bytes the controller receives.
 *
 * Panel id 255 (UNVERIFIED): panel_init() refuses before touching any pin or
 * the SPI bus, and every other call returns -ENODEV.
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
#define EPD_X0         DT_PROP(EPD, ram_x_offset)
#define EPD_PLANES     DT_PROP(EPD, planes)
#define EPD_FLAGS      DT_PROP(EPD, plane_flags)
#define EPD_TIMEOUT_MS DT_PROP(EPD, refresh_timeout_ms)
#define EPD_PLANE_LEN  ((EPD_W / 8) * EPD_H)

/* RAM: 400 sources x 300 gates; the window is set in whole bytes. */
BUILD_ASSERT(EPD_W % 8 == 0 && EPD_X0 % 8 == 0 && EPD_X0 + EPD_W <= 400 && EPD_H <= 300,
	     "SSD1619 geometry");
BUILD_ASSERT(DT_PROP(EPD, panel_id) != CTAG_PANEL_NONE, "the virtual panel is panel_uc8176.c's");

/* Commands (datasheet "Command Table"). */
#define SSD_DEEP_SLEEP 0x10u
#define SSD_DATA_ENTRY 0x11u
#define SSD_SW_RESET   0x12u
#define SSD_TSENSOR    0x18u
#define SSD_MASTER_ACT 0x20u
#define SSD_UPD_CTRL1  0x21u
#define SSD_UPD_CTRL2  0x22u
#define SSD_WRITE_BW   0x24u
#define SSD_WRITE_RED  0x26u
#define SSD_BORDER     0x3Cu
#define SSD_RAM_X      0x44u
#define SSD_RAM_Y      0x45u
#define SSD_RAM_XCOUNT 0x4Eu
#define SSD_RAM_YCOUNT 0x4Fu

/*
 * DISPLAY UPDATE CONTROL 1, first parameter: red RAM option in A[7:4], B/W
 * RAM option in A[3:0] (0 = normal, 4 = read as 0, 8 = inverse). Natively a
 * B/W bit 1 is white and a red bit 1 is red; one plane reads red as 0.
 */
#define RAM_NORMAL  0x0u
#define RAM_BYPASS0 0x4u
#define RAM_INVERSE 0x8u
#define FLAG_W1 ((EPD_FLAGS & 0x01) != 0) /* plane 0 bit 1 = white */
#define FLAG_R1 ((EPD_FLAGS & 0x02) != 0) /* plane 1 bit 1 = red */
#if EPD_PLANES == 1
#define RED_OPT RAM_BYPASS0
#else
#define RED_OPT (FLAG_R1 ? RAM_NORMAL : RAM_INVERSE)
#endif
#define BW_OPT   (FLAG_W1 ? RAM_NORMAL : RAM_INVERSE)
#define EPD_UPD1 ((RED_OPT << 4) | BW_OPT)

#define UPD2_FULL        0xF7u /* clock, analog, temperature, LUT, display, then off */
#define BORDER_WAVEFORM  0x01u
#define TSENSOR_INTERNAL 0x80u
#define ENTRY_X_THEN_Y   0x03u /* X and Y increment, X first: native row-major */
#define DEEP_SLEEP_MODE1 0x01u

#define RESET_MS     10
#define IDLE_WAIT_MS 500 /* SW reset, deep sleep: bounded blocking waits */
#define POLL_MS      50

static const struct spi_dt_spec spi =
	SPI_DT_SPEC_GET(EPD, SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER);
static const struct gpio_dt_spec dc = GPIO_DT_SPEC_GET(EPD, dc_gpios);
static const struct gpio_dt_spec rst = GPIO_DT_SPEC_GET(EPD, reset_gpios);
static const struct gpio_dt_spec busy_pin = GPIO_DT_SPEC_GET(EPD, busy_gpios);
#if DT_NODE_HAS_PROP(EPD, bs_gpios)
static const struct gpio_dt_spec bs = GPIO_DT_SPEC_GET(EPD, bs_gpios);
#endif
#if DT_NODE_HAS_PROP(EPD, en_gpios)
static const struct gpio_dt_spec en = GPIO_DT_SPEC_GET(EPD, en_gpios);
#endif

static bool usable;
static bool active;  /* reset and initialised since the last deep sleep */
static bool waiting; /* a refresh is running: panel_poll() watches BUSY */
static uint8_t cur_plane;
static uint16_t staged[EPD_PLANES];
static uint32_t refresh_t0;

/* BUSY read as a raw level; high = busy with busy-active-high. */
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

/* Address counters back to the window origin (the panel's first RAM byte). */
static int window_origin(void)
{
	uint8_t p[2] = {EPD_X0 / 8, 0u};
	int err = command(SSD_RAM_XCOUNT, p, 1);

	p[0] = 0u;
	return err ? err : command(SSD_RAM_YCOUNT, p, 2);
}

int panel_init(uint8_t panel_id)
{
	if (panel_id == CTAG_PANEL_UNVERIFIED || panel_id != DT_PROP(EPD, panel_id)) {
		return -ENODEV; /* no pin, no bus: placeholder or mismatched panel */
	}
#if DT_NODE_HAS_PROP(EPD, en_gpios)
	/* The supply first: the bus below drives CS high once it starts. */
	if (!gpio_is_ready_dt(&en) || gpio_pin_configure_dt(&en, GPIO_OUTPUT_ACTIVE) != 0) {
		return -ENODEV;
	}
#endif
	/* A zephyr,deferred-init bus starts here, after the panel supply. */
	if (!device_is_ready(spi.bus) && device_init(spi.bus) != 0) {
		return -ENODEV;
	}
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
	active = true;
	(void)gpio_pin_configure_dt(&busy_pin, GPIO_INPUT);
	hw_reset();
	err = command(SSD_SW_RESET, NULL, 0);
	if (err == 0 && !wait_idle()) {
		err = -EIO;
	}
	p[0] = BORDER_WAVEFORM;
	err = err ? err : command(SSD_BORDER, p, 1);
	p[0] = TSENSOR_INTERNAL;
	err = err ? err : command(SSD_TSENSOR, p, 1);
	p[0] = ENTRY_X_THEN_Y;
	err = err ? err : command(SSD_DATA_ENTRY, p, 1);
	p[0] = EPD_X0 / 8;
	p[1] = (EPD_X0 + EPD_W - 1) / 8;
	err = err ? err : command(SSD_RAM_X, p, 2);
	p[0] = 0u;
	p[1] = 0u;
	p[2] = (uint8_t)((EPD_H - 1) & 0xFF);
	p[3] = (uint8_t)((EPD_H - 1) >> 8);
	err = err ? err : command(SSD_RAM_Y, p, 4);
	err = err ? err : window_origin();
	return err ? err : command(SSD_WRITE_BW, NULL, 0);
}

int panel_write_plane_chunk(uint8_t plane, uint16_t offset, const uint8_t *data, size_t len)
{
	int err = 0;

	if (!usable || plane >= EPD_PLANES || plane < cur_plane || offset != staged[plane] ||
	    len > (size_t)(EPD_PLANE_LEN - offset)) {
		return -EINVAL;
	}
	if (plane != cur_plane) {
		err = window_origin(); /* the red RAM fills from the origin again */
		err = err ? err : command(SSD_WRITE_RED, NULL, 0);
	}
	err = err ? err : write_bytes(data, len);
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
	return !usable ? -ENODEV : busy() ? -EIO : 0;
}

int panel_commit_refresh(void)
{
	uint8_t p[2] = {EPD_UPD1, 0x00u};
	int err;

	if (!usable) {
		return -ENODEV;
	}
	refresh_t0 = k_uptime_get_32();
	err = command(SSD_UPD_CTRL1, p, 2);
	p[0] = UPD2_FULL;
	err = err ? err : command(SSD_UPD_CTRL2, p, 1);
	return err ? err : command(SSD_MASTER_ACT, NULL, 0);
}

void panel_poll(void)
{
	uint8_t status = CTAG_STATUS_OK;

	if (!waiting) {
		return;
	}
	if (busy()) {
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
	app_kick_after(POLL_MS);
	return 0;
}

void panel_sleep(void)
{
	uint8_t mode = DEEP_SLEEP_MODE1;

	if (!active) {
		return;
	}
	active = false;
	if (!wait_idle()) {
		hw_reset(); /* BUSY stuck (REFRESH_TIMEOUT): stop the controller */
	}
	(void)command(SSD_DEEP_SLEEP, &mode, 1);
	(void)gpio_pin_set_dt(&dc, 0);
	(void)gpio_pin_configure_dt(&busy_pin, GPIO_DISCONNECTED);
}

void panel_abort_frame(void)
{
	waiting = false;
	panel_sleep();
}
