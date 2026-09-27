"""ICU for the Unicode algorithms HarfBuzz does not do (docs/layout.md "Pipeline").

Everything here works on code-point indices of a Python ``str``. ICU works in
UTF-16 code units, so each helper converts through `Utf16Map`.

- grapheme cluster boundaries (UAX #29, ICU character break iterator),
- line break opportunities (UAX #14 + ICU's locale tailorings and dictionaries
  for Thai, Lao, Khmer, Myanmar and CJK),
- bidi embedding levels (UAX #9, ``icu.Bidi``), per paragraph and per line,
- script property and Script_Extensions, paired brackets, emoji properties.

Break iterators are expensive to create and are not thread-safe, so each thread
keeps its own per-locale instances.
"""

from __future__ import annotations

import threading
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

import icu

# Languages whose text is written right to left (BCP-47 primary subtags). Used
# only when a paragraph has no strong character to decide by itself.
RTL_LANGUAGES = frozenset({
    "ar", "arc", "ckb", "dv", "fa", "he", "iw", "ji", "ks", "ku-arab", "mzn", "nqo", "pnb", "ps", "sd", "syr",
    "ug", "ur", "yi", "azb", "bal", "bqi", "glk", "khw", "lrc", "sdh",
})

_BRACKET_NONE, _BRACKET_OPEN, _BRACKET_CLOSE = 0, 1, 2
_tls = threading.local()


def _iterators() -> dict[tuple[str, str], icu.BreakIterator]:
    cache = getattr(_tls, "iterators", None)
    if cache is None:
        cache = _tls.iterators = {}
    return cache


@lru_cache(maxsize=256)
def icu_locale(language: str) -> icu.Locale:
    """ICU locale for a BCP-47 tag (``""`` or an unparseable tag -> root; ``en_US`` is accepted)."""
    try:
        return icu.Locale.forLanguageTag(normalize_language(language) or "und")
    except icu.ICUError:
        return icu.Locale.getRoot()


@lru_cache(maxsize=256)
def normalize_language(language: object) -> str:
    """A usable BCP-47 tag: ``_`` -> ``-``, surrounding space removed; ``""`` when ICU cannot parse it."""
    if not isinstance(language, str):
        return ""
    tag = language.strip().replace("_", "-")
    if not tag:
        return ""
    try:
        parsed = icu.Locale.forLanguageTag(tag).toLanguageTag()
    except icu.ICUError:
        return ""
    return "" if parsed in ("und", "") else parsed


def primary_language(language: str) -> str:
    return (language or "").split("-", 1)[0].split("_", 1)[0].lower()


def is_rtl_language(language: str) -> bool:
    tag = (language or "").lower().replace("_", "-")
    if tag.startswith("ku-arab") or "-arab" in tag:
        return True
    if any(s in tag.split("-")[1:] for s in ("latn", "cyrl")):
        return False
    return primary_language(tag) in RTL_LANGUAGES


def _iterator(kind: str, language: str) -> icu.BreakIterator:
    cache = _iterators()
    key = (kind, language)
    it = cache.get(key)
    if it is None:
        loc = icu_locale(language)
        it = icu.BreakIterator.createLineInstance(loc) if kind == "line" else \
            icu.BreakIterator.createCharacterInstance(loc)
        cache[key] = it
    return it


@dataclass(frozen=True, slots=True)
class Utf16Map:
    """Code-point index <-> UTF-16 index for one string."""

    to16: tuple[int, ...]
    """``to16[i]`` = UTF-16 offset of code point ``i``; ``to16[n]`` = UTF-16 length."""

    @classmethod
    def of(cls, text: str) -> Utf16Map:
        out = [0] * (len(text) + 1)
        pos = 0
        for i, ch in enumerate(text):
            out[i] = pos
            pos += 2 if ord(ch) > 0xFFFF else 1
        out[len(text)] = pos
        return cls(tuple(out))

    @property
    def is_bmp(self) -> bool:
        return self.to16[-1] == len(self.to16) - 1

    def to_cp(self, offset16: int) -> int:
        """Code-point index of UTF-16 ``offset16`` (a boundary never splits a surrogate pair)."""
        if self.is_bmp:
            return offset16
        return bisect_right(self.to16, offset16) - 1


def graphemes(text: str, u16: Utf16Map | None = None) -> list[int]:
    """Extended grapheme cluster boundaries as code-point indices, from 0 to ``len(text)``."""
    if not text:
        return [0]
    u16 = u16 or Utf16Map.of(text)
    it = _iterator("char", "")
    it.setText(text)
    return [0] + [u16.to_cp(b) for b in it]


def line_break_locale(language: str) -> str:
    """ICU locale for line breaking: the primary language; Chinese and Japanese use ``lb=strict``
    (no line starts with small kana, the prolonged sound mark or iteration marks)."""
    primary = primary_language(language)
    if primary in ("ja", "zh", "yue"):
        return f"{primary}-u-lb-strict"
    return primary


def line_breaks(text: str, language: str, u16: Utf16Map | None = None) -> list[tuple[int, bool]]:
    """Line break opportunities ``(index, hard)`` after which a line may end (ICU, locale-tailored).

    The last entry is always ``len(text)``; ``hard`` marks mandatory breaks.
    """
    if not text:
        return [(0, True)]
    u16 = u16 or Utf16Map.of(text)
    it = _iterator("line", line_break_locale(language))
    it.setText(text)
    out = []
    it.first()
    while True:
        b = it.nextBoundary()
        if b == icu.BreakIterator.DONE:
            break
        out.append((u16.to_cp(b), it.getRuleStatus() >= 100))
    return out


def first_strong_direction(text: str) -> str | None:
    """``"ltr"`` / ``"rtl"`` from the first strong character (UAX #9 P2/P3), else None."""
    if not text:
        return None
    d = icu.Bidi.getBaseDirection(text)
    if d == icu.UBiDiDirection.LTR:
        return "ltr"
    if d == icu.UBiDiDirection.RTL:
        return "rtl"
    return None


def has_strong_rtl(text: str) -> bool:
    for ch in text:
        d = icu.Char.charDirection(ord(ch))
        if d in (icu.UCharDirection.RIGHT_TO_LEFT, icu.UCharDirection.RIGHT_TO_LEFT_ARABIC):
            return True
    return False


def paragraph_level(text: str, direction: str, language: str) -> int:
    """Paragraph embedding level: explicit ``direction``, else the text, else the language hint.

    ``auto``: an RTL language hint wins when the paragraph contains any strong
    RTL character (an English product name at the start of an Arabic sentence
    does not flip it); otherwise the first strong character decides; a
    paragraph without strong characters follows the language hint.
    """
    if direction == "ltr":
        return 0
    if direction == "rtl":
        return 1
    rtl_hint = is_rtl_language(language)
    if rtl_hint and has_strong_rtl(text):
        return 1
    first = first_strong_direction(text)
    if first is not None:
        return 1 if first == "rtl" else 0
    return 1 if rtl_hint else 0


class ParagraphBidi:
    """ICU bidi over one paragraph at a fixed paragraph level."""

    def __init__(self, text: str, level: int, u16: Utf16Map | None = None) -> None:
        self.text = text
        self.level = level
        self.u16 = u16 or Utf16Map.of(text)
        self._bidi = icu.Bidi()
        self._bidi.setPara(icu.UnicodeString(text), level)
        levels16 = list(self._bidi.getLevels()) if text else []
        self.levels = [levels16[self.u16.to16[i]] for i in range(len(text))]

    def line_levels(self, start: int, end: int) -> list[int]:
        """Levels of code points [start, end) as a line (rule L1 applied)."""
        if start >= end:
            return []
        line = self._bidi.setLine(self.u16.to16[start], self.u16.to16[end])
        levels16 = list(line.getLevels())
        base16 = self.u16.to16[start]
        return [levels16[self.u16.to16[i] - base16] for i in range(start, end)]


def visual_order(levels: Sequence[int]) -> list[int]:
    """Rule L2: indices of ``levels``' runs in visual order (left to right)."""
    order = list(range(len(levels)))
    if not levels:
        return order
    highest = max(levels)
    lowest = min(levels)
    lowest_odd = lowest if lowest % 2 else lowest + 1
    for level in range(highest, lowest_odd - 1, -1):
        i = 0
        while i < len(order):
            if levels[order[i]] >= level:
                j = i
                while j < len(order) and levels[order[j]] >= level:
                    j += 1
                order[i:j] = order[i:j][::-1]
                i = j
            else:
                i += 1
    return order


# --------------------------------------------------------------------------- character properties


@lru_cache(maxsize=65536)
def script_of(cp: int) -> str:
    """ISO 15924 Script of ``cp`` (``Zyyy`` Common, ``Zinh`` Inherited, ``Zzzz`` unknown)."""
    return icu.Script.getScript(cp).getShortName()


@lru_cache(maxsize=4096)
def _script_name(code: int) -> str:
    return icu.Script(code).getShortName()


@lru_cache(maxsize=65536)
def script_extensions(cp: int) -> tuple[str, ...]:
    """Script_Extensions of ``cp`` (its Script alone when it has none)."""
    return tuple(_script_name(c) for c in icu.Script.getScriptExtensions(cp))


@lru_cache(maxsize=4096)
def paired_bracket(cp: int) -> tuple[int, int]:
    """(type, pair): type 0 none, 1 open, 2 close; ``pair`` the matching bracket."""
    kind = icu.Char.getIntPropertyValue(cp, icu.UProperty.BIDI_PAIRED_BRACKET_TYPE)
    if kind == _BRACKET_NONE:
        return _BRACKET_NONE, cp
    return kind, icu.Char.getBidiPairedBracket(cp)


@lru_cache(maxsize=65536)
def _emoji_props(cp: int) -> tuple[bool, bool, bool, bool]:
    has = icu.Char.hasBinaryProperty
    return (has(cp, icu.UProperty.EMOJI_PRESENTATION), has(cp, icu.UProperty.EXTENDED_PICTOGRAPHIC),
            has(cp, icu.UProperty.EMOJI_MODIFIER), has(cp, icu.UProperty.REGIONAL_INDICATOR))


VS15, VS16, ZWJ, KEYCAP = 0xFE0E, 0xFE0F, 0x200D, 0x20E3


def emoji_presentation(cluster: Sequence[int]) -> bool | None:
    """True when a grapheme cluster asks for emoji presentation, False for text, None if not emoji.

    VS16, a keycap, a ZWJ sequence, a skin-tone modifier, a flag or an
    ``Emoji_Presentation`` base mean emoji; VS15 means text; any other
    ``Extended_Pictographic`` base defaults to text presentation.
    """
    if not cluster:
        return None
    if VS15 in cluster:
        return False
    base = _emoji_props(cluster[0])
    if VS16 in cluster or KEYCAP in cluster:
        return True
    if base[3]:
        return True
    if base[0]:
        return True
    if base[1]:
        if ZWJ in cluster or any(_emoji_props(cp)[2] for cp in cluster[1:]):
            return True
        return False
    return None


@lru_cache(maxsize=65536)
def general_category(cp: int) -> str:
    """Two-letter General_Category from ICU (Python's ``unicodedata`` may lag behind ICU's Unicode version)."""
    return icu.Char.getPropertyValueName(icu.UProperty.GENERAL_CATEGORY, icu.Char.charType(cp),
                                         icu.UPropertyNameChoice.SHORT_PROPERTY_NAME)


def nfc(text: str) -> str:
    """NFC with ICU's normalisation data."""
    return icu.Normalizer2.getNFCInstance().normalize(text) if text else text


def is_space(cp: int) -> bool:
    """Whitespace that a line may drop at its end (space separators and tab)."""
    return cp == 0x09 or cp == 0x20 or icu.Char.charType(cp) == icu.UCharCategory.SPACE_SEPARATOR
