"""PNG previews of layouts, rendered by the normative renderer (render/reference.py).

The image is exactly what a bridge draws — the same font pack, the same
§4.4 semantics and plane encoding — decoded back to colours: a black/white
panel gives a true 1-bit PNG, a black/white/red panel a 2-bit palette PNG
(white, black, red). By default the image is turned back to the logical
orientation (what a person reading the tag sees); ``orientation="native"``
keeps panel rows. ``scale`` enlarges pixels (nearest neighbour).

Cost: a 400x300 screen renders in roughly 0.1-0.3 s (pure Python) and
compresses to a few KiB; `preview_png` refuses to return more than
``MAX_PREVIEW_BYTES`` (the connector's 64 KiB limit) and retries at scale 1.
"""

from __future__ import annotations

import io
from typing import Literal

from PIL import Image

from cremind_tag.compose.api import ComposedScreen, TagPanel
from cremind_tag.fonts.fontset import FontSet
from cremind_tag.layout.fonts import FontContext
from cremind_tag.protocol.layout import Layout, decode_layout
from cremind_tag.render.reference import Frame, Panel, render_frame

MAX_PREVIEW_BYTES = 64 * 1024
PALETTE = (255, 255, 255, 0, 0, 0, 220, 0, 0)
DEFAULT_PLANE_FLAGS = 0x03  # plane 0: 1 = white; plane 1: 1 = red

Orientation = Literal["logical", "native"]


class PreviewTooLarge(ValueError):
    """The PNG would exceed the connector's preview limit."""


def _panel(panel: TagPanel | Panel | None, layout: Layout) -> Panel:
    if isinstance(panel, Panel):
        return panel
    if isinstance(panel, TagPanel):
        return Panel(panel.width, panel.height, panel.planes, panel.plane_flags)
    # A free-standing layout (text previews): unrotated, red plane only when it paints red.
    uses_red = bool(layout.flags & 1) or any(getattr(c, "color", 0) == 2 for c in layout.commands)
    w, h = (layout.width, layout.height) if layout.rotation in (0, 2) else (layout.height, layout.width)
    return Panel(w, h, 2 if uses_red else 1, DEFAULT_PLANE_FLAGS)


def frame_image(frame: Frame, panel: Panel) -> Image.Image:
    """Native-orientation image of a rendered frame (mode ``1`` or 3-colour ``P``)."""
    size = (panel.width, panel.height)
    plane0 = Image.frombytes("1", size, frame.planes[0])
    if not panel.plane_flags & 1:  # plane 0 bit 1 = black -> invert to "1 = white"
        plane0 = plane0.point(lambda v: 255 - v, "1")
    if panel.planes == 1:
        return plane0
    img = Image.new("P", size, 0)
    img.putpalette(PALETTE)
    black = plane0.point(lambda v: 255 - v, "1")
    img.paste(1, mask=black)
    red = Image.frombytes("1", size, frame.planes[1])
    if not panel.plane_flags & 2:
        red = red.point(lambda v: 255 - v, "1")
    img.paste(2, mask=red)
    return img


_TO_LOGICAL = {1: Image.Transpose.ROTATE_90, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_270}


def render_image(layout: bytes | Layout, fonts: FontSet, *, panel: TagPanel | Panel | None = None,
                 scale: int = 1, orientation: Orientation = "logical") -> Image.Image:
    """Render ``layout`` through the reference renderer and return the image."""
    if isinstance(layout, bytes | bytearray):
        layout = decode_layout(bytes(layout))
    rpanel = _panel(panel, layout)
    frame = render_frame(layout, rpanel, FontContext.for_fontset(fonts).pack)
    img = frame_image(frame, rpanel)
    if orientation == "logical" and layout.rotation in _TO_LOGICAL:
        img = img.transpose(_TO_LOGICAL[layout.rotation])
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.Resampling.NEAREST)
    return img


def png_bytes(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_png(layout: bytes | Layout, fonts: FontSet, *, panel: TagPanel | Panel | None = None, scale: int = 1,
               orientation: Orientation = "logical") -> bytes:
    """PNG bytes of ``layout`` (see `render_image`)."""
    return png_bytes(render_image(layout, fonts, panel=panel, scale=scale, orientation=orientation))


def preview_png(screen: ComposedScreen | bytes, panel: TagPanel, fonts: FontSet, *, scale: int = 1,
                orientation: Orientation = "logical", limit: int = MAX_PREVIEW_BYTES) -> bytes:
    """The connector preview (``POST previews``): PNG of a composed screen, at most ``limit`` bytes.

    A scaled image over the limit falls back to scale 1; a scale-1 image over
    the limit raises `PreviewTooLarge` (a 400x300 panel never gets close).
    """
    layout = screen.layout if isinstance(screen, ComposedScreen) else screen
    data = render_png(layout, fonts, panel=panel, scale=scale, orientation=orientation)
    if len(data) > limit and scale > 1:
        data = render_png(layout, fonts, panel=panel, scale=1, orientation=orientation)
    if len(data) > limit:
        raise PreviewTooLarge(f"preview PNG is {len(data)} bytes > {limit}")
    return data
