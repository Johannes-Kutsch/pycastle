from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath


class PycastleError(RuntimeError):
    pass


class RuntimeConfigurationError(PycastleError):
    pass


class ManagedWorktreeMountPreconditionError(PycastleError):
    def __init__(self, message: str, *, rejection_code: str) -> None:
        self.rejection_code = rejection_code
        super().__init__(message)


class AgentTimeoutError(PycastleError, TimeoutError):
    def __init__(
        self,
        message: str = "",
        role_value: str = "",
        worktree_path: Path | None = None,
    ) -> None:
        self.role_value = role_value
        self.worktree_path = worktree_path
        super().__init__(message)


class UsageLimitError(PycastleError):
    def __init__(
        self,
        reset_time: datetime | None = None,
        raw_message: str | None = None,
        provider: str | None = None,
        *,
        is_permanent: bool = False,
        account_label: str | None = None,
        stage_key: str | None = None,
    ) -> None:
        self.reset_time = reset_time
        self.raw_message = raw_message
        self.provider = provider
        self.is_permanent = is_permanent
        self.account_label = account_label
        self.stage_key = stage_key
        super().__init__(
            f"Usage limit reached (reset_time={reset_time.isoformat() if reset_time else None})"
        )


class ModelNotAvailableError(PycastleError):
    def __init__(
        self,
        service: str | None = None,
        model: str | None = None,
    ) -> None:
        self.service = service
        self.model = model
        super().__init__(f"Model not available (service={service!r}, model={model!r})")


class TransientAgentError(PycastleError):
    def __init__(self, message: str = "", status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class HardAgentError(PycastleError):
    def __init__(
        self,
        message: str = "",
        status_code: int | None = None,
        service_name: str = "",
        classification: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.caller = ""
        self.service_name = service_name
        self.classification = classification
        super().__init__(message)


class AgentCredentialFailureError(HardAgentError):
    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        service_name: str,
        classification: str | None = None,
    ) -> None:
        self.is_operator_actionable = True
        super().__init__(
            message=message,
            status_code=status_code,
            service_name=service_name,
            classification=classification,
        )


class AgentFailedError(PycastleError):
    session_store: str | Path
    worktree_path: Path

    def _legacy_session_store_path(
        self,
        role_value: str,
        namespace: str,
        service_name: str,
    ) -> str:
        role_root = PurePosixPath(".pycastle-session") / role_value
        if namespace:
            role_root = role_root / namespace
        if service_name:
            role_root = role_root / service_name
        return role_root.as_posix()

    def __init__(
        self,
        role_value: str,
        worktree_path: Path,
        namespace: str = "",
        failure_class: str = "",
        service_name: str = "",
        session_store: Path | str | None = None,
        agent_invocation_log_path: Path | str | None = None,
    ) -> None:
        super().__init__(f"Agent {role_value!r} failed irrecoverably")
        self.role_value = role_value
        self.worktree_path = worktree_path
        self.namespace = namespace
        self.failure_class = failure_class
        self.service_name = service_name
        self.agent_invocation_log_path = agent_invocation_log_path
        self.session_store = (
            session_store
            if session_store is not None
            else self._legacy_session_store_path(role_value, namespace, service_name)
        )


class DockerError(PycastleError):
    pass


class DockerTimeoutError(DockerError, TimeoutError):
    pass


class SetupPhaseError(PycastleError):
    def __init__(
        self,
        phase: str,
        message: str,
        *,
        command: str | None = None,
        output: str | None = None,
    ) -> None:
        self.phase = phase
        self.command = command
        self.output = output
        super().__init__(message)


class WorktreeError(PycastleError):
    pass


class WorktreeTimeoutError(WorktreeError, TimeoutError):
    pass


class BranchCollisionError(WorktreeError):
    pass


class ConfigValidationError(PycastleError):
    def __init__(
        self,
        message: str,
        *,
        invalid_value: str = "",
        suggestion: str = "",
        valid_options: list[str] | None = None,
    ) -> None:
        self.invalid_value = invalid_value
        self.suggestion = suggestion
        self.valid_options = valid_options or []
        super().__init__(message)


class ClaudeServiceError(PycastleError):
    pass


class ClaudeCliNotFoundError(ClaudeServiceError):
    pass


class ClaudeTimeoutError(ClaudeServiceError, TimeoutError):
    pass


class ClaudeCommandError(ClaudeServiceError):
    pass


class DockerServiceError(PycastleError):
    pass


class DockerBuildError(DockerServiceError):
    pass


__all__ = [
    "AgentCredentialFailureError",
    "AgentFailedError",
    "AgentTimeoutError",
    "BranchCollisionError",
    "ClaudeCliNotFoundError",
    "ClaudeCommandError",
    "ClaudeServiceError",
    "ClaudeTimeoutError",
    "ConfigValidationError",
    "DockerBuildError",
    "DockerError",
    "DockerServiceError",
    "DockerTimeoutError",
    "HardAgentError",
    "ManagedWorktreeMountPreconditionError",
    "ModelNotAvailableError",
    "PycastleError",
    "RuntimeConfigurationError",
    "SetupPhaseError",
    "TransientAgentError",
    "UsageLimitError",
    "WorktreeError",
    "WorktreeTimeoutError",
]
