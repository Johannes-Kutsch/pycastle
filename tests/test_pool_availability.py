from datetime import UTC, datetime, timedelta

import pytest

from pycastle.errors import UsageLimitError
from pycastle.services._pool_availability import PoolAvailabilityHelper

_FAR = datetime(2099, 1, 1, tzinfo=UTC).astimezone()
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).astimezone()


def _helper(
    *accounts: tuple[str, str], provider: str = "test-provider"
) -> PoolAvailabilityHelper:
    return PoolAvailabilityHelper(list(accounts), provider)


def test_pick_token_returns_highest_priority_credential():
    helper = _helper(("account 1", "tok-1"), ("account 2", "tok-2"))
    assert helper.pick_token() == "tok-1"


def test_pick_token_updates_current_token():
    helper = _helper(("account 1", "tok-1"), ("account 2", "tok-2"))
    helper.pick_token()
    # mark_exhausted acts on the current token; after picking tok-1, exhausting it
    # makes tok-2 the next available.
    helper.mark_exhausted(_FAR, now=_NOW)
    assert helper.pick_token() == "tok-2"


def test_mark_exhausted_makes_credential_unavailable_until_reset_time():
    reset_time = datetime(2099, 1, 1, 14, 30, tzinfo=UTC).astimezone()
    helper = _helper(("account 1", "tok-1"), provider="svc")
    helper.pick_token()
    helper.mark_exhausted(reset_time, now=_NOW)
    assert helper.is_available(now=_NOW) is False


def test_mark_exhausted_credential_becomes_available_after_reset():
    reset_time = datetime(2026, 1, 1, 12, 30, tzinfo=UTC).astimezone()
    after_reset = reset_time + timedelta(minutes=3)
    helper = _helper(("account 1", "tok-1"), provider="svc")
    helper.pick_token()
    helper.mark_exhausted(reset_time, now=_NOW)
    assert helper.is_available(now=_NOW) is False
    assert helper.is_available(now=after_reset) is True


def test_mark_permanently_exhausted_keeps_credential_unavailable():
    helper = _helper(("account 1", "tok-1"), provider="svc")
    helper.pick_token()
    helper.mark_permanently_exhausted()
    assert helper.is_available(now=_NOW) is False


def test_mark_permanently_exhausted_returns_account_name():
    helper = _helper(("my-account", "tok-1"), provider="svc")
    helper.pick_token()
    assert helper.mark_permanently_exhausted() == "my-account"


def test_model_restriction_observed_by_model_aware_availability():
    helper = _helper(("account 1", "tok-1"), provider="svc")
    helper.pick_token()
    helper.mark_model_restricted("sonnet")
    assert helper.is_available(now=_NOW, model="sonnet") is False
    assert helper.is_available(now=_NOW, model="haiku") is True


def test_model_restriction_does_not_affect_model_agnostic_availability():
    helper = _helper(("account 1", "tok-1"), provider="svc")
    helper.pick_token()
    helper.mark_model_restricted("sonnet")
    assert helper.is_available(now=_NOW) is True


def test_account_names_returns_names_in_configured_order():
    helper = _helper(("alpha", "tok-1"), ("beta", "tok-2"), ("gamma", "tok-3"))
    assert helper.account_names() == ["alpha", "beta", "gamma"]


def test_pick_token_raises_usage_limit_error_with_reset_time_when_exhausted():
    reset_time = datetime(2099, 1, 1, 14, 30, tzinfo=UTC).astimezone()
    helper = _helper(("account 1", "tok-1"), provider="myprovider")
    helper.pick_token()
    helper.mark_exhausted(reset_time, now=_NOW)

    with pytest.raises(UsageLimitError) as exc_info:
        helper.pick_token()

    err = exc_info.value
    assert err.provider == "myprovider"
    assert err.reset_time is not None
    assert not err.is_permanent


def test_pick_token_raises_permanent_usage_limit_error_when_all_permanently_exhausted():
    helper = _helper(("account 1", "tok-1"), provider="myprovider")
    helper.pick_token()
    helper.mark_permanently_exhausted()

    with pytest.raises(UsageLimitError) as exc_info:
        helper.pick_token()

    err = exc_info.value
    assert err.provider == "myprovider"
    assert err.is_permanent


def test_single_string_account_accepted():
    helper = PoolAvailabilityHelper("tok-single", provider="svc")
    assert helper.pick_token() == "tok-single"
    assert helper.account_names() == ["account 1"]


def test_next_wake_time_returns_earliest_finite_wake_time():
    reset_time = datetime(2099, 1, 1, 14, 30, tzinfo=UTC).astimezone()
    helper = _helper(("account 1", "tok-1"), provider="svc")
    helper.pick_token()
    helper.mark_exhausted(reset_time, now=_NOW)
    wake = helper.next_wake_time()
    assert wake > reset_time


def test_mutation_methods_are_no_ops_before_pick_token():
    helper = _helper(("account 1", "tok-1"), provider="svc")
    helper.mark_exhausted(_FAR, now=_NOW)
    helper.mark_model_restricted("sonnet")
    assert helper.mark_permanently_exhausted() is None
    assert helper.is_available(now=_NOW) is True
