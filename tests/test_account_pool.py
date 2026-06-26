from datetime import datetime, timedelta, timezone

import pytest

from pycastle.services.runtime_services import ClaudeService

# Use a far-future base so exhausted_until stays > now_local() during test runs.
_FAR = datetime(2099, 1, 1, tzinfo=timezone.utc).astimezone()


def _svc(*accounts: tuple[str, str]) -> ClaudeService:
    return ClaudeService(accounts=list(accounts))


def test_build_env_returns_primary_token_when_only_primary():
    svc = _svc(("primary", "tok-p"))
    env = svc.build_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-p"


def test_build_env_returns_first_account_token_when_multiple():
    svc = _svc(("secondary", "tok-s"), ("primary", "tok-p"))
    env = svc.build_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-s"


def test_build_env_skips_exhausted_account_and_returns_next():
    svc = _svc(("secondary", "tok-s"), ("primary", "tok-p"))
    svc.build_env()  # picks secondary as current
    svc.mark_exhausted(_FAR)  # wake = _FAR + 2 min; always in the future
    env = svc.build_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-p"


def test_is_available_true_when_one_account_unexhausted():
    now = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc).astimezone()
    reset = datetime(2026, 1, 1, 15, 0, 0, tzinfo=timezone.utc).astimezone()
    svc = _svc(("secondary", "tok-s"), ("primary", "tok-p"))
    svc.build_env()  # picks secondary
    svc.mark_exhausted(reset)  # wake = 15:02
    assert svc.is_available(now=now) is True


def test_is_available_false_when_all_accounts_exhausted():
    now = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc).astimezone()
    svc = _svc(("secondary", "tok-s"), ("primary", "tok-p"))

    # exhaust secondary (far-future so it stays exhausted for the next build_env call)
    svc.build_env()
    svc.mark_exhausted(_FAR)
    # exhaust primary
    svc.build_env()  # now picks primary (secondary exhausted far into the future)
    svc.mark_exhausted(_FAR)

    assert svc.is_available(now=now) is False


def test_is_available_true_again_after_wake_time_passes():
    early = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc).astimezone()
    reset = datetime(2026, 1, 1, 14, 30, 0, tzinfo=timezone.utc).astimezone()
    later = datetime(2026, 1, 1, 16, 0, 0, tzinfo=timezone.utc).astimezone()
    svc = _svc(("primary", "tok-p"))

    svc.build_env()
    svc.mark_exhausted(reset)  # wake = 14:32

    assert svc.is_available(now=early) is False
    assert svc.is_available(now=later) is True


def test_mark_exhausted_with_reset_time_sets_wake():
    reset = datetime(2026, 1, 1, 14, 50, 0, tzinfo=timezone.utc).astimezone()
    svc = _svc(("primary", "tok-p"))

    svc.build_env()
    svc.mark_exhausted(reset)

    assert svc.next_wake_time() is not None


def test_mark_exhausted_without_reset_time_sets_wake():
    now = datetime(2026, 1, 1, 14, 30, 0, tzinfo=timezone.utc).astimezone()
    svc = _svc(("primary", "tok-p"))

    svc.build_env()
    svc.mark_exhausted(None, _now=now)

    assert svc.next_wake_time() is not None


def test_next_wake_time_returns_min_over_exhausted_entries():
    # Use ordered far-future dates so exhaustion persists across build_env() calls
    early_reset = datetime(2099, 1, 1, 14, 30, 0, tzinfo=timezone.utc).astimezone()
    late_reset = datetime(2099, 1, 1, 16, 0, 0, tzinfo=timezone.utc).astimezone()
    svc = _svc(("secondary", "tok-s"), ("primary", "tok-p"))

    # exhaust secondary with late reset
    svc.build_env()  # picks secondary
    svc.mark_exhausted(late_reset)
    # exhaust primary with early reset
    svc.build_env()  # now picks primary (secondary exhausted until late_reset+2)
    svc.mark_exhausted(early_reset)

    assert svc.next_wake_time() == early_reset + timedelta(minutes=2)


def test_build_env_raises_when_all_accounts_exhausted():
    svc = _svc(("primary", "tok-p"))
    svc.build_env()
    svc.mark_exhausted(_FAR)

    with pytest.raises(RuntimeError, match="No available Claude accounts"):
        svc.build_env()  # no available account; _pool.pick raises


def test_empty_accounts_raises():
    with pytest.raises(ValueError, match="ClaudeService requires at least one account"):
        ClaudeService(accounts=[])


def test_mark_exhausted_without_prior_pick_is_noop():
    svc = _svc(("primary", "tok-p"))
    # No build_env() call → _current_token is None; mark_exhausted should be a no-op
    svc.mark_exhausted(None)
    assert svc.is_available() is True


def test_is_available_true_when_no_pool():
    svc = ClaudeService()
    assert svc.is_available() is True


def test_mark_exhausted_noop_when_no_pool():
    svc = ClaudeService()
    svc.mark_exhausted(None)  # must not raise
