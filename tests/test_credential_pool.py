from datetime import datetime, timedelta, timezone

import pytest

from pycastle.services.credential_pool import CredentialPool


_FAR = datetime(2099, 1, 1, tzinfo=timezone.utc).astimezone()
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).astimezone()


def _pool(*credentials: tuple[str, str]) -> CredentialPool:
    return CredentialPool(list(credentials))


def test_credential_pool_picks_first_available_credential():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    assert pool.pick() == ("account 1", "tok-1")


def test_credential_pool_skips_temporarily_exhausted_credential():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    pool.mark_exhausted("tok-1", _FAR)
    assert pool.pick() == ("account 2", "tok-2")


def test_credential_pool_marks_permanent_exhaustion_and_returns_name():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    assert pool.mark_permanently_exhausted("tok-1") == "account 1"
    assert pool.pick() == ("account 2", "tok-2")


def test_credential_pool_reports_availability_and_earliest_wake_time():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc).astimezone()
    early_reset = datetime(2099, 1, 1, 14, 30, tzinfo=timezone.utc).astimezone()
    late_reset = datetime(2099, 1, 1, 16, 0, tzinfo=timezone.utc).astimezone()
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))

    pool.mark_exhausted("tok-1", late_reset)
    assert pool.has_available(now=now) is True

    pool.mark_exhausted("tok-2", early_reset)
    assert pool.has_available(now=now) is False
    assert pool.earliest_wake_time() == early_reset + timedelta(minutes=2)


def test_credential_pool_uses_generic_default_errors():
    with pytest.raises(
        ValueError, match="CredentialPool requires at least one credential"
    ):
        CredentialPool([])

    pool = _pool(("account 1", "tok-1"))
    pool.mark_permanently_exhausted("tok-1")
    with pytest.raises(RuntimeError, match="No available credentials"):
        pool.pick()


def test_credential_pool_earliest_wake_time_raises_when_all_permanently_exhausted():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    pool.mark_permanently_exhausted("tok-1")
    pool.mark_permanently_exhausted("tok-2")

    with pytest.raises(RuntimeError):
        pool.earliest_wake_time()


def test_credential_pool_earliest_wake_time_skips_permanently_exhausted_accounts():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc).astimezone()
    reset_time = datetime(2099, 1, 1, 14, 30, tzinfo=timezone.utc).astimezone()
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    pool.mark_permanently_exhausted("tok-1")
    pool.mark_exhausted("tok-2", reset_time, now=now)

    assert pool.earliest_wake_time() == reset_time + timedelta(minutes=2)


# --- Model restriction tests ---


def test_model_restriction_makes_slot_unavailable_for_that_model():
    pool = _pool(("account 1", "tok-1"))
    pool.mark_model_restricted("tok-1", "sonnet")
    assert pool.has_available_for_model("sonnet", now=_NOW) is False


def test_model_restriction_does_not_affect_other_models_on_same_slot():
    pool = _pool(("account 1", "tok-1"))
    pool.mark_model_restricted("tok-1", "sonnet")
    assert pool.has_available_for_model("haiku", now=_NOW) is True


def test_model_restriction_does_not_affect_other_slots():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    pool.mark_model_restricted("tok-1", "sonnet")
    assert pool.has_available_for_model("sonnet", now=_NOW) is True


def test_model_restriction_scoped_to_slot_exhausted_slot_does_not_block_others():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    pool.mark_model_restricted("tok-1", "sonnet")
    pool.mark_exhausted("tok-1", _FAR, now=_NOW)
    assert pool.has_available_for_model("sonnet", now=_NOW) is True


def test_model_restriction_persists_when_slot_wakes_after_exhaustion():
    reset_time = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc).astimezone()
    after_reset = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc).astimezone()
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))

    pool.mark_model_restricted("tok-1", "sonnet")
    pool.mark_exhausted("tok-1", reset_time, now=_NOW)
    pool.mark_exhausted("tok-2", _FAR, now=_NOW)

    # After slot 1 wakes but slot 2 is still exhausted:
    # slot 1 is no longer exhausted, but its sonnet restriction persists.
    assert pool.has_available_for_model("sonnet", now=after_reset) is False
    # Other models are still available on woken slot 1.
    assert pool.has_available_for_model("haiku", now=after_reset) is True


def test_exhausting_slot_does_not_clear_model_restrictions():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    pool.mark_model_restricted("tok-1", "sonnet")
    pool.mark_permanently_exhausted("tok-1")
    # slot 2 is available, so sonnet is available there
    assert pool.has_available_for_model("sonnet", now=_NOW) is True
    # After exhausting slot 2 as well, neither slot can serve sonnet
    pool.mark_permanently_exhausted("tok-2")
    assert pool.has_available_for_model("sonnet", now=_NOW) is False


def test_has_available_without_model_is_unchanged_by_model_restrictions():
    pool = _pool(("account 1", "tok-1"))
    pool.mark_model_restricted("tok-1", "sonnet")
    assert pool.has_available(now=_NOW) is True


def test_pick_for_model_skips_restricted_slots():
    pool = _pool(("account 1", "tok-1"), ("account 2", "tok-2"))
    pool.mark_model_restricted("tok-1", "sonnet")
    assert pool.pick_for_model("sonnet", now=_NOW) == ("account 2", "tok-2")


def test_pick_for_model_raises_when_no_slot_available():
    pool = _pool(("account 1", "tok-1"))
    pool.mark_model_restricted("tok-1", "sonnet")
    with pytest.raises(RuntimeError, match="No available credentials"):
        pool.pick_for_model("sonnet", now=_NOW)
