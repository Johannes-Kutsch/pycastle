from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class _RemoteRetryDecision(Enum):
    RETRY_TRANSIENT = "retry_transient"
    ESCALATE_OPERATOR_ACTIONABLE = "escalate_operator_actionable"
    ESCALATE_RETRY_EXHAUSTED = "escalate_retry_exhausted"
    PASSTHROUGH_DIVERGENCE_OR_CONFLICT = "passthrough_divergence_or_conflict"
    RECOVER_PUSH_NON_FAST_FORWARD = "recover_push_non_fast_forward"


@dataclass(frozen=True)
class _RemoteRetryAction:
    decision: _RemoteRetryDecision
    delay_seconds: int | None = None


@dataclass(frozen=True)
class _RemoteRetryProfile:
    max_attempts: int
    backoff_seconds: tuple[int, ...]


_REMOTE_RETRY_PROFILE = _RemoteRetryProfile(
    max_attempts=4,
    backoff_seconds=(10, 60, 300),
)

_DIVERGENCE_OR_CONFLICT_PATTERNS = (
    "not possible to fast-forward",
    "need to specify how to reconcile divergent branches",
    "refusing to merge unrelated histories",
    "conflict",
)

_OPERATOR_ACTIONABLE_PATTERNS = (
    "repository not found",
    "remote: not found",
    "does not appear to be a git repository",
)

_NON_FAST_FORWARD_PUSH_PATTERNS = ("[rejected]",)


class _PrivateGitRemotePolicy:
    @property
    def max_attempts(self) -> int:
        return _REMOTE_RETRY_PROFILE.max_attempts

    def action_for_fetch_or_pull(self, stderr: str, attempt: int) -> _RemoteRetryAction:
        return self._action_from_decision(
            self._classify_fetch_or_pull(stderr, attempt), attempt
        )

    def action_for_push(self, stderr: str, attempt: int) -> _RemoteRetryAction:
        return self._action_from_decision(self._classify_push(stderr, attempt), attempt)

    def _action_from_decision(
        self, decision: _RemoteRetryDecision, attempt: int
    ) -> _RemoteRetryAction:
        if decision is _RemoteRetryDecision.RETRY_TRANSIENT:
            return _RemoteRetryAction(
                decision=decision,
                delay_seconds=_REMOTE_RETRY_PROFILE.backoff_seconds[attempt - 1],
            )
        return _RemoteRetryAction(decision=decision)

    def _classify_fetch_or_pull(
        self, stderr: str, attempt: int
    ) -> _RemoteRetryDecision:
        stderr_lower = stderr.lower()
        if any(pattern in stderr_lower for pattern in _OPERATOR_ACTIONABLE_PATTERNS):
            return _RemoteRetryDecision.ESCALATE_OPERATOR_ACTIONABLE
        if any(pattern in stderr_lower for pattern in _DIVERGENCE_OR_CONFLICT_PATTERNS):
            return _RemoteRetryDecision.PASSTHROUGH_DIVERGENCE_OR_CONFLICT
        if attempt >= _REMOTE_RETRY_PROFILE.max_attempts:
            return _RemoteRetryDecision.ESCALATE_RETRY_EXHAUSTED
        return _RemoteRetryDecision.RETRY_TRANSIENT

    def _classify_push(self, stderr: str, attempt: int) -> _RemoteRetryDecision:
        stderr_lower = stderr.lower()
        if any(pattern in stderr_lower for pattern in _OPERATOR_ACTIONABLE_PATTERNS):
            return _RemoteRetryDecision.ESCALATE_OPERATOR_ACTIONABLE
        if any(pattern in stderr for pattern in _NON_FAST_FORWARD_PUSH_PATTERNS):
            return _RemoteRetryDecision.RECOVER_PUSH_NON_FAST_FORWARD
        if any(pattern in stderr_lower for pattern in _DIVERGENCE_OR_CONFLICT_PATTERNS):
            return _RemoteRetryDecision.PASSTHROUGH_DIVERGENCE_OR_CONFLICT
        if attempt >= _REMOTE_RETRY_PROFILE.max_attempts:
            return _RemoteRetryDecision.ESCALATE_RETRY_EXHAUSTED
        return _RemoteRetryDecision.RETRY_TRANSIENT


_PRIVATE_GIT_REMOTE_POLICY = _PrivateGitRemotePolicy()
