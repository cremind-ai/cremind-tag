"""Multilingual text layout for tags: plain text -> positioned glyphs -> GLYPHS commands.

See docs/layout.md. Entry points:

- `plain_text` — card text (Markdown / HTML-ish) -> plain NFC text;
- `layout_text` — paragraph layout in a box (ICU graphemes, bidi, line breaks;
  script itemisation; font fallback; HarfBuzz shaping and line-boundary
  reshaping; ellipsis; alignment) -> `TextBlock` with `LineBox`es and
  `PositionedGlyph`s;
- `glyph_commands` / `TextBlock.commands` — GLYPHS commands with i8 deltas.
"""

from .commands import command_glyphs, glyph_commands
from .engine import LineBox, PositionedGlyph, TextBlock, layout_text, measure_text
from .fonts import FontContext
from .plaintext import plain_text

__all__ = [
    "FontContext",
    "LineBox",
    "PositionedGlyph",
    "TextBlock",
    "command_glyphs",
    "glyph_commands",
    "layout_text",
    "measure_text",
    "plain_text",
]
