"""Local times for screens: ICU date formatting in the profile's language and time zone.

Skeletons, not patterns, so every locale gets its own order, separators,
12/24-hour convention, calendar and digits (``jm`` -> "2:05 PM" in en,
"14:05" in vi, "۱۴:۰۵" in fa). An unknown time zone falls back to UTC.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from functools import lru_cache

import icu

from cremind_tag.layout.unicode import icu_locale

TIME = "jm"
"""Hour and minute, locale's 12/24-hour preference."""
DATE_TIME = "MMMEdjm"
"""Weekday, day, abbreviated month, hour and minute."""
SHORT_DATE = "MMMd"
DATE_SHORT_TIME = "MMMdjm"
_DAY = "yyyyMMdd"

_lock = threading.Lock()


@lru_cache(maxsize=64)
def _zone(name: str) -> icu.TimeZone:
    tz = icu.TimeZone.createTimeZone(name or "UTC")
    if tz.getID() == "Etc/Unknown":
        tz = icu.TimeZone.createTimeZone("UTC")
    return tz


@lru_cache(maxsize=256)
def _formatter(language: str, zone: str, skeleton: str) -> icu.SimpleDateFormat:
    loc = icu_locale(language or "en")
    if skeleton == _DAY:
        fmt = icu.SimpleDateFormat(_DAY, icu.Locale("en_US_POSIX"))
    else:
        pattern = icu.DateTimePatternGenerator.createInstance(loc).getBestPattern(skeleton)
        fmt = icu.SimpleDateFormat(pattern, loc)
    fmt.setTimeZone(_zone(zone))
    return fmt


def _seconds(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def format_time(dt: datetime, language: str, zone: str, skeleton: str = TIME) -> str:
    """``dt`` in ``zone`` formatted for ``language`` with an ICU date-time skeleton."""
    fmt = _formatter(language, zone, skeleton)
    with _lock:
        return str(fmt.format(_seconds(dt)))


def same_local_day(a: datetime, b: datetime, zone: str) -> bool:
    return format_time(a, "en", zone, _DAY) == format_time(b, "en", zone, _DAY)


def card_stamp(ts: datetime, now: datetime, language: str, zone: str, *, long: bool = False) -> str:
    """A card's time: "14:05" today, "27 Sep" on another local day ("27 Sep, 14:05" with ``long``)."""
    if same_local_day(ts, now, zone):
        return format_time(ts, language, zone, TIME)
    return format_time(ts, language, zone, DATE_SHORT_TIME if long else SHORT_DATE)


def parse_timestamp(value: object) -> datetime | None:
    """An ISO 8601 card timestamp (``2026-09-27T10:00:00Z``) as an aware datetime, else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
