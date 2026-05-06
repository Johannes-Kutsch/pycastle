from datetime import datetime, timedelta

import pytest

from pycastle.account_pool import AccountPool


def test_pick_returns_first_account_when_only_primary():
    pool = AccountPool([("primary", "tok-p")])

    assert pool.pick() == ("primary", "tok-p")


def test_pick_prefers_first_account_over_second():
    pool = AccountPool([("secondary", "tok-s"), ("primary", "tok-p")])

    assert pool.pick() == ("secondary", "tok-s")


def test_pick_skips_exhausted_account_and_returns_next():
    now = datetime(2026, 1, 1, 14, 0, 0)
    reset = datetime(2026, 1, 1, 15, 0, 0)
    pool = AccountPool([("secondary", "tok-s"), ("primary", "tok-p")])

    pool.mark_exhausted("tok-s", reset, now=now)

    assert pool.pick(now=now) == ("primary", "tok-p")


def test_has_available_true_when_one_account_unexhausted():
    now = datetime(2026, 1, 1, 14, 0, 0)
    reset = datetime(2026, 1, 1, 15, 0, 0)
    pool = AccountPool([("secondary", "tok-s"), ("primary", "tok-p")])

    pool.mark_exhausted("tok-s", reset, now=now)

    assert pool.has_available(now=now) is True


def test_has_available_false_when_all_accounts_exhausted():
    now = datetime(2026, 1, 1, 14, 0, 0)
    reset = datetime(2026, 1, 1, 15, 0, 0)
    pool = AccountPool([("secondary", "tok-s"), ("primary", "tok-p")])

    pool.mark_exhausted("tok-s", reset, now=now)
    pool.mark_exhausted("tok-p", reset, now=now)

    assert pool.has_available(now=now) is False


def test_has_available_true_again_after_wake_time_passes():
    early = datetime(2026, 1, 1, 14, 0, 0)
    reset = datetime(2026, 1, 1, 14, 30, 0)
    later = datetime(2026, 1, 1, 16, 0, 0)
    pool = AccountPool([("primary", "tok-p")])

    pool.mark_exhausted("tok-p", reset, now=early)

    assert pool.has_available(now=later) is True


def test_mark_exhausted_with_reset_time_uses_reset_plus_two_min():
    now = datetime(2026, 1, 1, 14, 0, 0)
    reset = datetime(2026, 1, 1, 14, 50, 0)
    pool = AccountPool([("primary", "tok-p")])

    pool.mark_exhausted("tok-p", reset, now=now)

    assert pool.earliest_wake_time() == reset + timedelta(minutes=2)


def test_mark_exhausted_without_reset_time_uses_next_hour_plus_two_min():
    now = datetime(2026, 1, 1, 14, 30, 0)
    expected = datetime(2026, 1, 1, 15, 2, 0)
    pool = AccountPool([("primary", "tok-p")])

    pool.mark_exhausted("tok-p", None, now=now)

    assert pool.earliest_wake_time() == expected


def test_earliest_wake_time_returns_min_over_exhausted_entries():
    now = datetime(2026, 1, 1, 14, 0, 0)
    early_reset = datetime(2026, 1, 1, 14, 30, 0)
    late_reset = datetime(2026, 1, 1, 16, 0, 0)
    pool = AccountPool([("secondary", "tok-s"), ("primary", "tok-p")])

    pool.mark_exhausted("tok-s", late_reset, now=now)
    pool.mark_exhausted("tok-p", early_reset, now=now)

    assert pool.earliest_wake_time() == early_reset + timedelta(minutes=2)


def test_pick_with_no_available_accounts_raises():
    now = datetime(2026, 1, 1, 14, 0, 0)
    reset = datetime(2026, 1, 1, 15, 0, 0)
    pool = AccountPool([("primary", "tok-p")])
    pool.mark_exhausted("tok-p", reset, now=now)

    with pytest.raises(RuntimeError):
        pool.pick(now=now)


def test_empty_accounts_raises():
    with pytest.raises(ValueError):
        AccountPool([])


def test_mark_exhausted_unknown_token_is_idempotent_noop():
    now = datetime(2026, 1, 1, 14, 0, 0)
    pool = AccountPool([("primary", "tok-p")])

    pool.mark_exhausted("tok-other", None, now=now)

    assert pool.has_available(now=now) is True
