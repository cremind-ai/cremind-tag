/*
 * Panel interface of docs/protocol.md 6 (per panel profile). Writes stage
 * image bytes in the controller's RAM and never trigger a refresh; only
 * panel_commit_refresh() issues the refresh command. Every function returns 0
 * or a negative errno (the core reports PANEL_ERROR).
 *
 * Implemented by panel_uc8176.c or panel_ssd1619.c on the tag (the controller
 * of the chosen panel node) and by a fake in the native_sim tests. Planes arrive in order (plane 0 fully, then plane 1) at strictly
 * increasing offsets; a frame always restarts from begin_frame().
 */
#ifndef TAG_PANEL_H_
#define TAG_PANEL_H_

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Boot: configure the pins of a usable panel (never for panel id 255). */
int panel_init(uint8_t panel_id);

/* Reset and initialise the controller for a new frame (restart-safe). */
int panel_begin_frame(void);

/* Stage len bytes of plane at offset (the bytes already staged for it). */
int panel_write_plane_chunk(uint8_t plane, uint16_t offset, const uint8_t *data, size_t len);

/* Every byte of every plane staged and the controller idle. */
int panel_validate_frame(void);

/* Power the panel and issue the refresh command. */
int panel_commit_refresh(void);

/*
 * Arm the non-blocking BUSY poll bounded by the devicetree refresh timeout.
 * The result arrives later through tag_core_refresh_done() (OK or
 * REFRESH_TIMEOUT), from panel_poll().
 */
int panel_wait_refresh_complete(void);

/* Called from the core work item: checks BUSY while a refresh runs. */
void panel_poll(void);

/* Power off and deep-sleep the controller after a refresh. */
void panel_sleep(void);

/* Drop a partly staged frame: the panel never refreshes it. */
void panel_abort_frame(void);

#ifdef __cplusplus
}
#endif

#endif /* TAG_PANEL_H_ */
