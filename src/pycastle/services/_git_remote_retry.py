from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _RemoteRetryAction:
    retry_delay_seconds: int | None = None
    operator_actionable: bool = False
    passthrough: bool = False
    recover_push_non_fast_forward: bool = False

    @classmethod
    def retry_transient(cls, delay_seconds: int) -> "_RemoteRetryAction":
        return cls(retry_delay_seconds=delay_seconds)

    @classmethod
    def escalate_operator_actionable(cls) -> "_RemoteRetryAction":
        return cls(operator_actionable=True)

    @classmethod
    def passthrough_divergence_or_conflict(cls) -> "_RemoteRetryAction":
        return cls(passthrough=True)

    @classmethod
    def recover_push_rejection(cls) -> "_RemoteRetryAction":
        return cls(recover_push_non_fast_forward=True)


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
        stderr_lower = stderr.lower()
        if any(pattern in stderr_lower for pattern in _OPERATOR_ACTIONABLE_PATTERNS):
            return _RemoteRetryAction.escalate_operator_actionable()
        if any(pattern in stderr_lower for pattern in _DIVERGENCE_OR_CONFLICT_PATTERNS):
            return _RemoteRetryAction.passthrough_divergence_or_conflict()
        if attempt >= _REMOTE_RETRY_PROFILE.max_attempts:
            return _RemoteRetryAction.escalate_operator_actionable()
        return _RemoteRetryAction.retry_transient(
            _REMOTE_RETRY_PROFILE.backoff_seconds[attempt - 1]
        )

    def action_for_push(self, stderr: str, attempt: int) -> _RemoteRetryAction:
        stderr_lower = stderr.lower()
        if any(pattern in stderr_lower for pattern in _OPERATOR_ACTIONABLE_PATTERNS):
            return _RemoteRetryAction.escalate_operator_actionable()
        if any(pattern in stderr for pattern in _NON_FAST_FORWARD_PUSH_PATTERNS):
            return _RemoteRetryAction.recover_push_rejection()
        if any(pattern in stderr_lower for pattern in _DIVERGENCE_OR_CONFLICT_PATTERNS):
            return _RemoteRetryAction.passthrough_divergence_or_conflict()
        if attempt >= _REMOTE_RETRY_PROFILE.max_attempts:
            return _RemoteRetryAction.escalate_operator_actionable()
        return _RemoteRetryAction.retry_transient(
            _REMOTE_RETRY_PROFILE.backoff_seconds[attempt - 1]
        )


_PRIVATE_GIT_REMOTE_POLICY = _PrivateGitRemotePolicy()
