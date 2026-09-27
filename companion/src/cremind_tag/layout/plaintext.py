"""Plain-text extraction for card text (docs/layout.md "Plain text").

Cards arrive with whatever text Cremind produced: mostly plain, sometimes with
Markdown or HTML-ish markup from chat replies. A tag has one font style, so
markup is removed, keeping the words a reader would see:

- HTML: ``<br>`` and block-closing tags become line breaks, ``<script>`` /
  ``<style>`` bodies are dropped, other tags are removed, entities decoded;
- Markdown: fenced and inline code keep their text, links and images keep their
  label / alt text, emphasis markers, headings, block quotes, bullets, rules,
  table pipes and reference definitions are removed, backslash escapes resolved;
- controls go (tab = space), whitespace runs collapse to one space, the result
  is NFC (runs of ASCII spaces collapse; no-break and ideographic spaces are
  kept). ``keep_newlines`` keeps single line breaks between paragraphs (card
  bodies); otherwise everything is one paragraph (titles).
"""

from __future__ import annotations

import html
import re
import unicodedata

from .unicode import nfc

_BLOCK_DROP = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BR = re.compile(r"<br\s*/?>|</(?:p|div|li|h[1-6]|tr|blockquote|pre|ul|ol|table)\s*>", re.IGNORECASE)
_AUTOLINK = re.compile(r"<((?:https?|mailto):[^\s<>]+)>", re.IGNORECASE)
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

_FENCE = re.compile(r"^[ \t]*(```|~~~)[^\n]*\n(.*?)(?:^[ \t]*\1[ \t]*$|\Z)", re.MULTILINE | re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_REF_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_REF_DEF = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", re.MULTILINE)
_CODE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_EM_STAR = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", re.DOTALL)
_EM_UNDER = re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", re.DOTALL)
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.MULTILINE)
_SETEXT = re.compile(r"^[ \t]*(?:=+|-{2,})[ \t]*$", re.MULTILINE)
_QUOTE = re.compile(r"^[ \t]*(?:>[ \t]?)+", re.MULTILINE)
_BULLET = re.compile(r"^[ \t]*[-*+•][ \t]+", re.MULTILINE)
_RULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$", re.MULTILINE)
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>|~<])")
_ESCAPED_BASE = 0xF0000
_ESCAPED = re.compile("[\U000F0000-\U000F007F]")
# ASCII spaces collapse; no-break, ideographic and other typographic spaces are content and stay.
_SPACES = re.compile(r"[ \t]+")
_NEWLINES = re.compile(r"[ ]*\n[ \n]*")


def _strip_html(text: str) -> str:
    text = _COMMENT.sub("", text)
    text = _BLOCK_DROP.sub("", text)
    text = _BR.sub("\n", text)
    text = _AUTOLINK.sub(r"\1", text)
    text = _TAG.sub("", text)
    return html.unescape(text)


def _strip_markdown(text: str) -> str:
    # Backslash escapes are protected first (as plane-15 private-use stand-ins) so that
    # ``\*`` never opens emphasis; they are restored as the literal character at the end.
    protect = not _ESCAPED.search(text)
    if protect:
        text = _ESCAPE.sub(lambda m: chr(_ESCAPED_BASE + ord(m.group(1))), text)
    text = _FENCE.sub(lambda m: m.group(2), text)
    text = _REF_DEF.sub("", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _REF_LINK.sub(r"\1", text)
    text = _CODE.sub(lambda m: m.group(2).strip(), text)
    text = _TABLE_SEP.sub("", text)
    text = _RULE.sub("", text)
    text = _HEADING.sub(r"\1", text)
    text = _SETEXT.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    for pattern in (_STRONG, _STRIKE):
        text = pattern.sub(lambda m: m.group(m.lastindex or 0), text)
    text = _EM_STAR.sub(r"\1", text)
    text = _EM_UNDER.sub(r"\1", text)
    # Table rows: pipes between cells become spaces.
    text = re.sub(r"^[ \t]*\|(.*)\|[ \t]*$", lambda m: m.group(1).replace("|", "  "), text, flags=re.MULTILINE)
    if protect:
        return _ESCAPED.sub(lambda m: chr(ord(m.group(0)) - _ESCAPED_BASE), text)
    return _ESCAPE.sub(r"\1", text)


def _clean_controls(text: str) -> str:
    out = []
    for ch in text:
        if ch == "\n":
            out.append(ch)
        elif ch in "\t\v\f\r\x85  ":
            out.append("\n" if ch in "\v\f\x85  " else " ")
        elif unicodedata.category(ch) == "Cc":
            continue
        else:
            out.append(ch)
    return "".join(out)


def plain_text(text: str | None, *, keep_newlines: bool = False, markup: bool = True) -> str:
    """Card text -> what the tag shows (see the module docstring)."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if markup:
        text = _strip_markdown(_strip_html(text))
    text = _clean_controls(nfc(text))
    text = _SPACES.sub(" ", text)
    text = _NEWLINES.sub("\n", text).strip()
    if not keep_newlines:
        text = text.replace("\n", " ")
    return nfc(text)
