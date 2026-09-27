"""Defence in depth: the companion refuses cards whose text must never reach a screen.

Cremind already redacts what it journals (``app/tags/sanitize.py``: one-time
codes, bearer/API tokens, ``key=…`` pairs, long base64/hex runs) and never
journals tool or terminal output (docs/cremind-integration.md §3). This
validator mirrors those rules and **refuses** — never rewrites — a card that
still carries such text, because it means something upstream went wrong. A
refused job is receipted ``failed`` with detail ``refused_by_companion``
(docs/security.md "Content policy"); the text itself is never logged.

Checked: the card's ``title`` and ``body``, each as given AND as the tag would
show it (``layout.plaintext.plain_text``: HTML entities decoded, HTML and
Markdown stripped, spaces collapsed; with and without its line breaks), and a
whitespace-collapsed copy of every form, as Cremind's ``contains_otp`` does.
Rules:

- one-time codes: a 4–8 digit number (or ``123 456``) within 24 characters of
  ``otp``/``code``/``passcode``/``pin``/``verification``/… on either side; the
  gap may cross a line break (``Your code:\\n482913``);
- credentials: ``Bearer …``/``CremindTag …``/``token …`` values, well-known
  token shapes (``sk-…``, ``ghp_…``, ``xox?-…``, ``AIza…``, ``tagc_…``, JWTs),
  ``password=…``/``api_key: …`` pairs, hex runs ≥ 24 and base64 runs ≥ 24 mixing
  upper case, lower case and digits;
- raw tool/terminal output: ANSI escape sequences, code fences, Python
  tracebacks, shell prompts at the start of a line (``$ cmd``, ``user@host:~$``,
  ``PS C:\\…>``, ``C:\\…>``, ``>>>``), or three or more log-formatted lines.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_OTP_WORDS = (
    r"(?:otp|one[\s-]?time(?:\s+(?:code|password|passcode|pin))?|pass\s?code|pin|"
    r"verification(?:\s+code)?|security\s+code|login\s+code|auth(?:entication)?\s+code|"
    r"2fa|mfa|code|m[aã]\s+(?:x[aá]c\s+(?:nh[aậ]n|th[uự]c)|otp))"
)
_OTP_DIGITS = r"(\d{4,8}|\d{3}[\s-]\d{3})"
# The gap may cross a line break: "Your code:\n482913" is still a code (Cremind's rule).
_OTP_AFTER = re.compile(rf"(?i)\b{_OTP_WORDS}\b[^\d]{{0,24}}?{_OTP_DIGITS}(?!\d)")
_OTP_BEFORE = re.compile(rf"(?i)(?<!\d){_OTP_DIGITS}\b[^\d]{{0,24}}?\b{_OTP_WORDS}\b")
_WS = re.compile(r"\s+")

_SCHEME_TOKEN = re.compile(r"(?i)\b(bearer|cremindtag|basic|token)\s+[A-Za-z0-9._~+/=:-]{8,}")
_KNOWN_TOKENS = re.compile(
    r"\b(?:"
    r"(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|tagc_[a-z0-9]{10,}(?:\.[A-Za-z0-9_-]+)?"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
_KEY_VALUE = re.compile(
    r"(?i)\b([\w.-]*(?:api[_-]?key|access[_-]?key|private[_-]?key|token|secret|password|passwd|pwd|"
    r"passcode|key))\s*[:=]\s*(?![\"']?\[redacted\])(\"[^\"]+\"|'[^']+'|[^\s,;&]+)"
)  # Cremind's own redaction (``password=[redacted]``) is fine
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{24,}\b")
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_FENCE = re.compile(r"```")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")
_PROMPT = re.compile(
    r"(?m)^\s*(?:\$\s+\S|[\w.-]+@[\w.-]+:[^\n$#]*[$#]\s|PS [A-Za-z]:\\[^>\n]*>\s|[A-Za-z]:\\[^>\n]*>\s*\S|>>>\s)"
)
_LOG_LINE = re.compile(r"(?m)^\s*\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[^\n]*\b(?:DEBUG|INFO|WARN(?:ING)?|ERROR|"
                       r"CRITICAL|TRACE|FATAL)\b")

REFUSED_DETAIL = "refused_by_companion"
TEXT_FIELDS = ("title", "body")


def _mixed_b64(run: str) -> bool:
    upper = sum(c.isupper() for c in run)
    lower = sum(c.islower() for c in run)
    digits = sum(c.isdigit() for c in run)
    return upper >= 2 and lower >= 2 and digits >= 2


def text_problem(text: str) -> str | None:
    """Why ``text`` must not be displayed (``None`` when it may)."""
    if not text:
        return None
    for candidate in (text, _WS.sub(" ", text)):
        if _OTP_AFTER.search(candidate) or _OTP_BEFORE.search(candidate):
            return "one-time code"
    if _SCHEME_TOKEN.search(text) or _KNOWN_TOKENS.search(text) or _KEY_VALUE.search(text):
        return "credential"
    if _HEX_RUN.search(text) or any(_mixed_b64(m.group(0)) for m in _B64_RUN.finditer(text)):
        return "credential-like opaque run"
    if _ANSI.search(text) or _FENCE.search(text) or _TRACEBACK.search(text) or _PROMPT.search(text):
        return "tool or terminal output"
    if len(_LOG_LINE.findall(text)) >= 3:
        return "tool or terminal output"
    return None


def displayed_forms(value: str) -> list[str]:
    """``value`` as given and as a tag would show it (plain text, with and without its line breaks)."""
    from ..layout.plaintext import plain_text

    forms = [value]
    for keep_newlines in (False, True):
        try:
            shown = plain_text(value, keep_newlines=keep_newlines)
        except Exception:  # the raw value is still checked
            continue
        if shown and shown not in forms:
            forms.append(shown)
    return forms


def card_problem(card: Mapping[str, Any] | None) -> str | None:
    """Why a job's ``card`` must be refused (``None`` when it is acceptable)."""
    if not isinstance(card, Mapping):
        return None
    for key in TEXT_FIELDS:
        value = card.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return f"{key} is not text"
        for form in displayed_forms(value):
            problem = text_problem(form)
            if problem is not None:
                return f"{problem} in {key}"
    return None


__all__ = ["REFUSED_DETAIL", "card_problem", "displayed_forms", "text_problem"]
