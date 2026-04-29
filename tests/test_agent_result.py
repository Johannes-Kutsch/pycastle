import dataclasses

import pytest

from pycastle.agent_result import (
    AgentIncomplete,
    AgentResult,
    AgentSuccess,
    CancellationToken,
    PreflightFailure,
    PromiseParseFailure,
    UsageLimitHit,
)


# ── AgentSuccess ──────────────────────────────────────────────────────────────


def test_agent_success_stores_output():
    result = AgentSuccess(output="done")
    assert result.output == "done"


def test_agent_success_is_frozen():
    result = AgentSuccess(output="done")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.output = "other"  # type: ignore[misc]


# ── AgentIncomplete ───────────────────────────────────────────────────────────


def test_agent_incomplete_stores_partial_output():
    result = AgentIncomplete(partial_output="half done")
    assert result.partial_output == "half done"


def test_agent_incomplete_is_frozen():
    result = AgentIncomplete(partial_output="half done")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.partial_output = "other"  # type: ignore[misc]


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


# ── UsageLimitHit ─────────────────────────────────────────────────────────────


def test_usage_limit_hit_stores_last_output():
    result = UsageLimitHit(last_output="last line")
    assert result.last_output == "last line"


def test_usage_limit_hit_is_frozen():
    result = UsageLimitHit(last_output="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.last_output = "other"  # type: ignore[misc]


# ── PromiseParseFailure ───────────────────────────────────────────────────────


def test_promise_parse_failure_stores_fields():
    result = PromiseParseFailure(raw_output="x", detail="no promise tag")
    assert result.raw_output == "x"
    assert result.detail == "no promise tag"


def test_promise_parse_failure_is_frozen():
    result = PromiseParseFailure(raw_output="x", detail="d")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.detail = "other"  # type: ignore[misc]


# ── AgentResult union ─────────────────────────────────────────────────────────


def test_agent_result_covers_all_variants():
    variants: list[AgentResult] = [
        AgentSuccess(output="ok"),
        AgentIncomplete(partial_output="partial"),
        PreflightFailure(failures=()),
        UsageLimitHit(last_output=""),
        PromiseParseFailure(raw_output="", detail=""),
    ]
    assert len(variants) == 5


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
