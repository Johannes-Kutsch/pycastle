from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class _RetryTransient:
    delay_seconds: int


@dataclass(frozen=True)
class _EscalateOperatorActionable:
    pass


@dataclass(frozen=True)
class _PassthroughDivergenceOrConflict:
    pass


@dataclass(frozen=True)
class _RecoverPushNonFastForward:
    pass


_RemoteOperation = Literal["fetch", "pull", "push"]
_RemoteRetryDecision = (
    _RetryTransient
    | _EscalateOperatorActionable
    | _PassthroughDivergenceOrConflict
    | _RecoverPushNonFastForward
)


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

    def decision_for(
        self,
        operation: _RemoteOperation,
        stderr: str,
        attempt: int,
    ) -> _RemoteRetryDecision:
        stderr_lower = stderr.lower()
        if any(pattern in stderr_lower for pattern in _OPERATOR_ACTIONABLE_PATTERNS):
            return _EscalateOperatorActionable()
        if operation == "push" and any(
            pattern in stderr for pattern in _NON_FAST_FORWARD_PUSH_PATTERNS
        ):
            return _RecoverPushNonFastForward()
        if any(pattern in stderr_lower for pattern in _DIVERGENCE_OR_CONFLICT_PATTERNS):
            return _PassthroughDivergenceOrConflict()
        if attempt >= _REMOTE_RETRY_PROFILE.max_attempts:
            return _EscalateOperatorActionable()
        return _RetryTransient(
            delay_seconds=_REMOTE_RETRY_PROFILE.backoff_seconds[attempt - 1]
        )


_PRIVATE_GIT_REMOTE_POLICY = _PrivateGitRemotePolicy()
