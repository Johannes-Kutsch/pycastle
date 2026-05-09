import pytest

from pycastle.agent_result import CancellationToken
from pycastle.errors import PreflightFailure


# ── PreflightFailure ──────────────────────────────────────────────────────────


def test_preflight_failure_stores_failures():
    failures = (("check", "cmd", "output"),)
    exc = PreflightFailure(failures=failures)
    assert exc.failures == failures


def test_preflight_failure_is_exception():
    exc = PreflightFailure(failures=())
    assert isinstance(exc, Exception)


def test_preflight_failure_failures_are_immutable():
    exc = PreflightFailure(failures=(("check", "cmd", "output"),))
    with pytest.raises(TypeError):
        exc.failures[0] = ("x", "y", "z")  # type: ignore[index]


# ── CancellationToken ─────────────────────────────────────────────────────────


def test_cancellation_token_starts_uncancelled():
    token = CancellationToken()
    assert not token.is_cancelled


def test_cancellation_token_cancel_sets_is_cancelled():
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled


def test_cancellation_token_second_cancel_is_idempotent():
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.is_cancelled


def test_cancellation_token_constructor_takes_no_arguments():
    token = CancellationToken()
    assert not token.is_cancelled
