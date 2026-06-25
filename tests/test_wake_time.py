from datetime import datetime, timedelta

from pycastle.services._wake_time import compute_wake_time


def test_reset_time_supplied_returns_reset_plus_two_minutes() -> None:
    reset = datetime(2026, 5, 19, 14, 58)
    now = datetime(2026, 5, 19, 14, 0)
    wake, is_estimated = compute_wake_time(reset, now)
    assert wake == datetime(2026, 5, 19, 15, 0)
    assert is_estimated is False


def test_reset_time_in_the_past_still_used() -> None:
    reset = datetime(2026, 5, 19, 10, 0)
    now = datetime(2026, 5, 19, 14, 37)
    wake, is_estimated = compute_wake_time(reset, now)
    assert wake == reset + timedelta(minutes=2)
    assert is_estimated is False


def test_none_reset_uses_next_top_of_hour_plus_two() -> None:
    now = datetime(2026, 5, 19, 14, 37, 21, 999)
    wake, is_estimated = compute_wake_time(None, now)
    assert wake == datetime(2026, 5, 19, 15, 2)
    assert is_estimated is True


def test_none_reset_at_end_of_day_rolls_into_next_day() -> None:
    now = datetime(2026, 5, 19, 23, 59, 59)
    wake, is_estimated = compute_wake_time(None, now)
    assert wake == datetime(2026, 5, 20, 0, 2)
    assert is_estimated is True


def test_none_reset_at_exact_top_of_hour_rolls_forward() -> None:
    now = datetime(2026, 5, 19, 14, 0, 0)
    wake, is_estimated = compute_wake_time(None, now)
    assert wake == datetime(2026, 5, 19, 15, 2)
    assert is_estimated is True


def test_unknown_reset_minimum_duration_rounds_up_to_next_supported_wake_boundary() -> (
    None
):
    now = datetime(2026, 5, 19, 14, 30, 0)
    wake, is_estimated = compute_wake_time(
        None,
        now,
        minimum_unknown_reset_duration=timedelta(hours=1, minutes=30),
    )
    assert wake == datetime(2026, 5, 19, 16, 2)
    assert is_estimated is True


def test_reset_time_remains_authoritative_even_with_minimum_duration() -> None:
    reset = datetime(2026, 5, 19, 15, 30, 0)
    now = datetime(2026, 5, 19, 14, 30, 0)
    wake, is_estimated = compute_wake_time(
        reset,
        now,
        minimum_unknown_reset_duration=timedelta(hours=6),
    )
    assert wake == datetime(2026, 5, 19, 15, 32, 0)
    assert is_estimated is False
