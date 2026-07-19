#!/usr/bin/env python3
"""Timezone conversion with a zero-dependency Windows fallback."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
FIXED_OFFSETS = {
    "UTC": (0, "UTC"),
    "Etc/UTC": (0, "UTC"),
    "Asia/Shanghai": (8, "CST"),
    "Asia/Hong_Kong": (8, "HKT"),
    "Asia/Taipei": (8, "CST"),
    "Asia/Seoul": (9, "KST"),
    "Asia/Tokyo": (9, "JST"),
}


def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def _last_sunday(year: int, month: int) -> dt.date:
    if month == 12:
        last = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - 6) % 7)


def _us_offset(utc_value: dt.datetime, standard_hours: int, daylight_hours: int,
               start_utc_hour: int, end_utc_hour: int,
               standard_name: str, daylight_name: str) -> tuple[int, str]:
    year = utc_value.year
    start = dt.datetime.combine(_nth_sunday(year, 3, 2), dt.time(start_utc_hour), tzinfo=UTC)
    end = dt.datetime.combine(_nth_sunday(year, 11, 1), dt.time(end_utc_hour), tzinfo=UTC)
    return (daylight_hours, daylight_name) if start <= utc_value < end else (standard_hours, standard_name)


def _fallback_offset(utc_value: dt.datetime, name: str) -> tuple[int, str]:
    if name in FIXED_OFFSETS:
        return FIXED_OFFSETS[name]
    if name == "America/New_York":
        return _us_offset(utc_value, -5, -4, 7, 6, "EST", "EDT")
    if name == "America/Chicago":
        return _us_offset(utc_value, -6, -5, 8, 7, "CST", "CDT")
    if name == "America/Los_Angeles":
        return _us_offset(utc_value, -8, -7, 10, 9, "PST", "PDT")
    if name in {"Europe/Berlin", "Europe/Paris"}:
        start = dt.datetime.combine(_last_sunday(utc_value.year, 3), dt.time(1), tzinfo=UTC)
        end = dt.datetime.combine(_last_sunday(utc_value.year, 10), dt.time(1), tzinfo=UTC)
        return (2, "CEST") if start <= utc_value < end else (1, "CET")
    if name == "Europe/London":
        start = dt.datetime.combine(_last_sunday(utc_value.year, 3), dt.time(1), tzinfo=UTC)
        end = dt.datetime.combine(_last_sunday(utc_value.year, 10), dt.time(1), tzinfo=UTC)
        return (1, "BST") if start <= utc_value < end else (0, "GMT")
    if name == "Australia/Sydney":
        year = utc_value.year
        october_start = dt.datetime.combine(_nth_sunday(year, 10, 1) - dt.timedelta(days=1), dt.time(16), tzinfo=UTC)
        april_end = dt.datetime.combine(_nth_sunday(year, 4, 1) - dt.timedelta(days=1), dt.time(16), tzinfo=UTC)
        return (11, "AEDT") if utc_value < april_end or utc_value >= october_start else (10, "AEST")
    return (0, "UTC")


@lru_cache(maxsize=None)
def _zoneinfo(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def timezone_supported(name: str) -> bool:
    return _zoneinfo(name) is not None or name in FIXED_OFFSETS or name in {
        "America/New_York", "America/Chicago", "America/Los_Angeles",
        "Europe/Berlin", "Europe/Paris", "Europe/London", "Australia/Sydney",
    }


def localize_utc(utc_value: dt.datetime, name: str, prefer_zoneinfo: bool = True) -> dt.datetime:
    if utc_value.tzinfo is None:
        utc_value = utc_value.replace(tzinfo=UTC)
    utc_value = utc_value.astimezone(UTC)
    if prefer_zoneinfo:
        zone = _zoneinfo(name)
        if zone is not None:
            return utc_value.astimezone(zone)
    hours, label = _fallback_offset(utc_value, name)
    return utc_value.astimezone(dt.timezone(dt.timedelta(hours=hours), label))


def now_in_timezone(name: str, prefer_zoneinfo: bool = True) -> dt.datetime:
    return localize_utc(dt.datetime.now(UTC), name, prefer_zoneinfo)


def timezone_label_for_date(name: str, date_value: dt.date, prefer_zoneinfo: bool = True) -> str:
    # Noon UTC is sufficient for a stable date-level DST label used in headings.
    probe = dt.datetime.combine(date_value, dt.time(17), tzinfo=UTC)
    return localize_utc(probe, name, prefer_zoneinfo).tzname() or "UTC"
