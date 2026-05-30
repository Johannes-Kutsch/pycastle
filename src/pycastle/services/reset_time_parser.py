from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from enum import StrEnum

from .. import _time as _time_module

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


class ResetTimeSyntaxMode(StrEnum):
    CLAUDE_RESETS_UTC = "claude_resets_utc"
    TRY_AGAIN_UTC_OPTIONAL_DATE = "try_again_utc_optional_date"
    TRY_AGAIN_UTC_REQUIRED_DATE = "try_again_utc_required_date"


_PATTERNS = {
    ResetTimeSyntaxMode.CLAUDE_RESETS_UTC: re.compile(
        r"resets\s+"
        r"(?:(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+)?"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)\s+\(UTC\)",
        re.IGNORECASE,
    ),
    ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE: re.compile(
        r"(?:or\s+)?try again at\s+"
        r"(?:(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s+(?P<year>\d{4})\s+)?"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
        re.IGNORECASE,
    ),
    ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE: re.compile(
        r"try again at\s+"
        r"(?:(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s+(?P<year>\d{4})\s+)?"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
        re.IGNORECASE,
    ),
}


def parse_reset_time(
    retry_text: str,
    syntax_mode: ResetTimeSyntaxMode,
    *,
    now: datetime | None = None,
) -> datetime | None:
    match = _PATTERNS[syntax_mode].search(retry_text)
    if match is None:
        return None

    local_now = now or _time_module.now_local()
    utc_now = local_now.astimezone(timezone.utc)

    hour = _parse_hour(match.group("hour"), match.group("ampm"))
    minute = _parse_minute(match.group("minute"))
    if hour is None or minute is None:
        return None

    if syntax_mode is ResetTimeSyntaxMode.CLAUDE_RESETS_UTC:
        return _parse_claude_reset(match, local_now, utc_now, hour, minute)
    if syntax_mode is ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE:
        return _parse_optional_date_reset(match, local_now, utc_now, hour, minute)
    return _parse_required_date_reset(match, local_now, hour, minute)


def _parse_hour(hour_text: str, ampm_text: str) -> int | None:
    hour = int(hour_text)
    if not 1 <= hour <= 12:
        return None

    ampm = ampm_text.lower()
    if ampm == "pm" and hour != 12:
        return hour + 12
    if ampm == "am" and hour == 12:
        return 0
    return hour


def _parse_minute(minute_text: str | None) -> int | None:
    minute = int(minute_text or 0)
    if 0 <= minute <= 59:
        return minute
    return None


def _parse_month(month_text: str | None) -> int | None:
    if month_text is None:
        return None
    return _MONTHS.get(month_text.lower())


def _parse_claude_reset(
    match: re.Match[str],
    local_now: datetime,
    utc_now: datetime,
    hour: int,
    minute: int,
) -> datetime | None:
    local_tz = local_now.tzinfo
    month_text = match.group("month")
    day = match.group("day")
    month = _parse_month(month_text)
    if month_text is not None or day is not None:
        if month is None or day is None:
            return None
        utc_dt = _build_utc_datetime(utc_now.year, month, int(day), hour, minute)
        if utc_dt is None:
            return None
        local_dt = utc_dt.astimezone(local_tz)
        if local_dt < local_now - timedelta(days=31):
            rolled = _build_utc_datetime(utc_dt.year + 1, month, int(day), hour, minute)
            if rolled is None:
                return None
            return rolled.astimezone(local_tz)
        return local_dt

    return _parse_same_day_utc_reset(local_now, utc_now, hour, minute, local_tz)


def _parse_optional_date_reset(
    match: re.Match[str],
    local_now: datetime,
    utc_now: datetime,
    hour: int,
    minute: int,
) -> datetime | None:
    local_tz = local_now.tzinfo
    year = match.group("year")
    month = _parse_month(match.group("month"))
    day = match.group("day")
    if year is not None and month is not None and day is not None:
        utc_dt = _build_utc_datetime(int(year), month, int(day), hour, minute)
        if utc_dt is None:
            return None
        return utc_dt.astimezone(local_tz)

    return _parse_same_day_utc_reset(local_now, utc_now, hour, minute, local_tz)


def _parse_same_day_utc_reset(
    local_now: datetime,
    utc_now: datetime,
    hour: int,
    minute: int,
    local_tz: tzinfo | None,
) -> datetime | None:
    utc_dt = _combine_utc_date(utc_now.date(), hour, minute)
    if utc_dt is None:
        return None
    if utc_dt < utc_now - timedelta(minutes=2):
        utc_dt = _combine_utc_date(utc_now.date() + timedelta(days=1), hour, minute)
        if utc_dt is None:
            return None
    return utc_dt.astimezone(local_tz)


def _parse_required_date_reset(
    match: re.Match[str], local_now: datetime, hour: int, minute: int
) -> datetime | None:
    year = match.group("year")
    month = _parse_month(match.group("month"))
    day = match.group("day")
    if year is None or month is None or day is None:
        return None

    utc_dt = _build_utc_datetime(int(year), month, int(day), hour, minute)
    if utc_dt is None:
        return None
    return utc_dt.astimezone(local_now.tzinfo)


def _combine_utc_date(base_date: date, hour: int, minute: int) -> datetime | None:
    try:
        return datetime.combine(base_date, time(hour, minute), tzinfo=timezone.utc)
    except ValueError:
        return None


def _build_utc_datetime(
    year: int, month: int, day: int, hour: int, minute: int
) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
