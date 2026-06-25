from datetime import datetime, timedelta, timezone

import pytest

from pycastle.services.credential_pool import CredentialPool


_FAR = datetime(2099, 1, 1, tzinfo=timezone.utc).astimezone()


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
