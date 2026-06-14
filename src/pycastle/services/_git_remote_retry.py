from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RetryTransientRemoteFailure:
    delay_seconds: int


@dataclass(frozen=True)
class EscalateOperatorActionableGitFailure:
    pass


@dataclass(frozen=True)
class PassthroughRemoteDivergenceOrConflict:
    pass


@dataclass(frozen=True)
class RecoverPushNonFastForward:
    pass


RemoteGitOperation = Literal["fetch", "pull", "push"]
RemoteGitRetryDecision = (
    RetryTransientRemoteFailure
    | EscalateOperatorActionableGitFailure
    | PassthroughRemoteDivergenceOrConflict
    | RecoverPushNonFastForward
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


class RemoteGitRetryPolicy:
    @property
    def max_attempts(self) -> int:
        return _REMOTE_RETRY_PROFILE.max_attempts

    def classify_remote_failure(
        self,
        operation: RemoteGitOperation,
        stderr: str,
        attempt: int,
    ) -> RemoteGitRetryDecision:
        stderr_lower = stderr.lower()
        if any(pattern in stderr_lower for pattern in _OPERATOR_ACTIONABLE_PATTERNS):
            return EscalateOperatorActionableGitFailure()
        if operation == "push" and any(
            pattern in stderr for pattern in _NON_FAST_FORWARD_PUSH_PATTERNS
        ):
            return RecoverPushNonFastForward()
        if any(pattern in stderr_lower for pattern in _DIVERGENCE_OR_CONFLICT_PATTERNS):
            return PassthroughRemoteDivergenceOrConflict()
        if attempt >= _REMOTE_RETRY_PROFILE.max_attempts:
            return EscalateOperatorActionableGitFailure()
        return RetryTransientRemoteFailure(
            delay_seconds=_REMOTE_RETRY_PROFILE.backoff_seconds[attempt - 1]
        )


DEFAULT_REMOTE_GIT_RETRY_POLICY = RemoteGitRetryPolicy()

_RetryTransient = RetryTransientRemoteFailure
_EscalateOperatorActionable = EscalateOperatorActionableGitFailure
_PassthroughDivergenceOrConflict = PassthroughRemoteDivergenceOrConflict
_RecoverPushNonFastForward = RecoverPushNonFastForward
_RemoteOperation = RemoteGitOperation
_RemoteRetryDecision = RemoteGitRetryDecision
_PRIVATE_GIT_REMOTE_POLICY = DEFAULT_REMOTE_GIT_RETRY_POLICY
