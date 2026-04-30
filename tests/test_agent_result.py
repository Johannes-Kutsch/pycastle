import dataclasses

import pytest

from pycastle.agent_result import (
    CancellationToken,
    PreflightFailure,
)


# ── PreflightFailure ──────────────────────────────────────────────────────────


def test_preflight_failure_stores_failures():
    failures = (("check", "cmd", "output"),)
    result = PreflightFailure(failures=failures)
    assert result.failures == failures


def test_preflight_failure_is_frozen():
    result = PreflightFailure(failures=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.failures = ()  # type: ignore[misc]


def test_preflight_failure_failures_are_immutable():
    result = PreflightFailure(failures=(("check", "cmd", "output"),))
    with pytest.raises(TypeError):
        result.failures[0] = ("x", "y", "z")  # type: ignore[index]


# ── CancellationToken ─────────────────────────────────────────────────────────


def test_cancellation_token_starts_uncancelled():
    token = CancellationToken()
    assert not token.is_cancelled
    assert not token.wants_worktree_preserved


def test_cancellation_token_cancel_sets_is_cancelled():
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled


def test_cancellation_token_cancel_without_preserve_does_not_set_preserve():
    token = CancellationToken()
    token.cancel()
    assert not token.wants_worktree_preserved


def test_cancellation_token_cancel_with_preserve_sets_wants_worktree_preserved():
    token = CancellationToken()
    token.cancel(preserve_worktree=True)
    assert token.is_cancelled
    assert token.wants_worktree_preserved


def test_cancellation_token_second_cancel_is_idempotent():
    token = CancellationToken()
    token.cancel(preserve_worktree=True)
    token.cancel()  # second call without preserve should not clear it
    assert token.is_cancelled
    assert token.wants_worktree_preserved


def test_cancellation_token_cancel_with_preserve_after_plain_cancel_is_idempotent():
    token = CancellationToken()
    token.cancel()
    token.cancel(preserve_worktree=True)  # second call should not set preserve
    assert token.is_cancelled
    assert not token.wants_worktree_preserved


def test_cancellation_token_constructor_takes_no_arguments():
    token = CancellationToken()
    assert not token.is_cancelled
    assert not token.wants_worktree_preserved
