from __future__ import annotations

from datetime import datetime, timedelta, timezone


from pycastle.services import ResetTimeSyntaxMode, parse_reset_time

_UTC = timezone.utc

# ── helpers ───────────────────────────────────────────────────────────────────


def _utc(result: datetime | None) -> datetime:
    assert result is not None
    return result.astimezone(_UTC)


# ── CLAUDE_RESETS_UTC ─────────────────────────────────────────────────────────


def test_claude_resets_utc_no_match_returns_none():
    result = parse_reset_time(
        "no reset info here",
        ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
        now=datetime(2026, 5, 27, 14, 0, tzinfo=_UTC),
    )
    assert result is None


def test_claude_resets_utc_time_only_in_future():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets 3pm (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
        )
    )
    assert (utc.year, utc.month, utc.day, utc.hour, utc.minute) == (2026, 5, 27, 15, 0)


def test_claude_resets_utc_time_only_with_minute():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets 3:30pm (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
        )
    )
    assert (utc.hour, utc.minute) == (15, 30)


def test_claude_resets_utc_12pm_is_noon():
    now = datetime(2026, 5, 27, 10, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets 12pm (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
        )
    )
    assert (utc.hour, utc.minute) == (12, 0)


def test_claude_resets_utc_12am_is_midnight_rolls_over():
    # 12am = 00:00 UTC, now = 01:00 UTC — midnight has passed, so rollover to next day
    now = datetime(2026, 5, 27, 1, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets 12am (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
        )
    )
    assert (utc.hour, utc.minute) == (0, 0)
    assert utc.day == 28


def test_claude_resets_utc_same_day_rollover_when_time_passed():
    # 3pm UTC already passed; now = 16:00 UTC
    now = datetime(2026, 5, 27, 16, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets 3pm (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
        )
    )
    assert utc.day == 28
    assert (utc.hour, utc.minute) == (15, 0)


def test_claude_resets_utc_no_rollover_within_two_minute_window():
    # 3pm UTC is only 1 min in the past — within the 2-min grace window, no rollover
    now = datetime(2026, 5, 27, 15, 1, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets 3pm (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
        )
    )
    assert utc.day == 27
    assert utc.hour == 15


def test_claude_resets_utc_with_date_in_future():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets May 28, 3pm (UTC)",
            ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
            now=now,
        )
    )
    assert (utc.year, utc.month, utc.day, utc.hour) == (2026, 5, 28, 15)


def test_claude_resets_utc_with_date_more_than_31_days_in_past_rolls_year():
    # Jan 15 is > 31 days before May 27 — year should advance by one
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "limit resets Jan 15, 3pm (UTC)",
            ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
            now=now,
        )
    )
    assert utc.year == 2027
    assert (utc.month, utc.day, utc.hour) == (1, 15, 15)


def test_claude_resets_utc_abbreviated_month():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "resets Feb 28, 3pm (UTC)",
            ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
            now=now,
        )
    )
    # Feb 28 is > 31 days before May 27 in the same year — expect 2027
    assert utc.year == 2027
    assert (utc.month, utc.day) == (2, 28)


def test_claude_resets_utc_invalid_date_feb30_returns_none():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "resets Feb 30, 3pm (UTC)",
        ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
        now=now,
    )
    assert result is None


def test_claude_resets_utc_invalid_hour_zero_returns_none():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "resets 0pm (UTC)",
        ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
        now=now,
    )
    assert result is None


def test_claude_resets_utc_invalid_hour_13_returns_none():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "limit resets 13:00am (UTC)",
        ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
        now=now,
    )
    assert result is None


def test_claude_resets_utc_returns_aware_datetime():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "resets 3pm (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
    )
    assert result is not None
    assert result.tzinfo is not None


def test_claude_resets_utc_case_insensitive():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "RESETS 3PM (UTC)", ResetTimeSyntaxMode.CLAUDE_RESETS_UTC, now=now
    )
    assert result is not None


def test_claude_resets_utc_embedded_in_longer_text():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "Your plan limit resets 3:30pm (UTC) daily.",
            ResetTimeSyntaxMode.CLAUDE_RESETS_UTC,
            now=now,
        )
    )
    assert (utc.hour, utc.minute) == (15, 30)


# ── TRY_AGAIN_UTC_OPTIONAL_DATE ───────────────────────────────────────────────


def test_try_again_optional_date_no_match_returns_none():
    result = parse_reset_time(
        "no usage limit here",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
        now=datetime(2026, 5, 27, 14, 0, tzinfo=_UTC),
    )
    assert result is None


def test_try_again_optional_date_time_only_in_future():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "try again at 3:30 PM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
            now=now,
        )
    )
    assert (utc.year, utc.month, utc.day, utc.hour, utc.minute) == (2026, 5, 27, 15, 30)


def test_try_again_optional_date_or_prefix_accepted():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "You've hit your limit. Please wait or try again at 3:30 PM.",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
            now=now,
        )
    )
    assert (utc.hour, utc.minute) == (15, 30)


def test_try_again_optional_date_same_day_rollover():
    now = datetime(2026, 5, 27, 16, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "try again at 3:30 PM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
            now=now,
        )
    )
    assert utc.day == 28
    assert (utc.hour, utc.minute) == (15, 30)


def test_try_again_optional_date_no_rollover_within_two_minute_window():
    now = datetime(2026, 5, 27, 15, 31, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "try again at 3:30 PM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
            now=now,
        )
    )
    assert utc.day == 27
    assert (utc.hour, utc.minute) == (15, 30)


def test_try_again_optional_date_with_full_date():
    now = datetime(2026, 3, 15, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "try again at March 15th, 2026 3:30 PM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
            now=now,
        )
    )
    assert (utc.year, utc.month, utc.day, utc.hour, utc.minute) == (2026, 3, 15, 15, 30)


def test_try_again_optional_date_ordinal_suffixes_accepted():
    now = datetime(2026, 3, 1, 10, 0, tzinfo=_UTC)
    for suffix in ("st", "nd", "rd", "th"):
        text = f"try again at March 1{suffix}, 2026 3:30 PM"
        utc = _utc(
            parse_reset_time(
                text, ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE, now=now
            )
        )
        assert (utc.month, utc.day) == (3, 1)


def test_try_again_optional_date_invalid_date_feb30_returns_none():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "try again at February 30th, 2026 3:30 PM",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
        now=now,
    )
    assert result is None


def test_try_again_optional_date_invalid_hour_returns_none():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "try again at 13:30 PM",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
        now=now,
    )
    assert result is None


def test_try_again_optional_date_returns_aware_datetime():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "try again at 3:30 PM",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE,
        now=now,
    )
    assert result is not None
    assert result.tzinfo is not None


# ── TRY_AGAIN_UTC_REQUIRED_DATE ──────────────────────────────────────────────


def test_try_again_required_date_no_match_returns_none():
    result = parse_reset_time(
        "no usage limit here",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
        now=datetime(2026, 5, 27, 14, 0, tzinfo=_UTC),
    )
    assert result is None


def test_try_again_required_date_time_only_returns_none():
    # Date components are required — time-only text must return None
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "try again at 3:30 PM",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
        now=now,
    )
    assert result is None


def test_try_again_required_date_with_full_date():
    now = datetime(2026, 3, 15, 14, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "try again at March 15th, 2026 3:30 PM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
            now=now,
        )
    )
    assert (utc.year, utc.month, utc.day, utc.hour, utc.minute) == (2026, 3, 15, 15, 30)


def test_try_again_required_date_september_aliases_preserve_local_display_time():
    local_tz = timezone(timedelta(hours=-7))
    now = datetime(2026, 9, 15, 7, 0, tzinfo=local_tz)
    results = [
        parse_reset_time(
            f"try again at {month} 15th, 2026 3:30 PM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
            now=now,
        )
        for month in ("Sept", "September", "sep")
    ]

    assert [
        (result.hour, result.minute, result.utcoffset())
        for result in results
        if result is not None
    ] == [(8, 30, timedelta(hours=-7))] * 3


def test_try_again_required_date_invalid_date_returns_none():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "try again at February 30th, 2026 3:30 PM",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
        now=now,
    )
    assert result is None


def test_try_again_required_date_unknown_month_returns_none():
    now = datetime(2026, 5, 27, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "try again at Octember 15th, 2026 3:30 PM",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
        now=now,
    )
    assert result is None


def test_try_again_required_date_returns_aware_datetime():
    now = datetime(2026, 3, 15, 14, 0, tzinfo=_UTC)
    result = parse_reset_time(
        "try again at March 15th, 2026 3:30 PM",
        ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
        now=now,
    )
    assert result is not None
    assert result.tzinfo is not None


def test_try_again_required_date_12pm_is_noon():
    now = datetime(2026, 3, 15, 10, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "try again at March 15th, 2026 12:00 PM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
            now=now,
        )
    )
    assert utc.hour == 12


def test_try_again_required_date_12am_is_midnight():
    now = datetime(2026, 3, 15, 10, 0, tzinfo=_UTC)
    utc = _utc(
        parse_reset_time(
            "try again at March 16th, 2026 12:00 AM",
            ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE,
            now=now,
        )
    )
    assert utc.hour == 0
