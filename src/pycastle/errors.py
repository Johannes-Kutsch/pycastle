from pycastle_agent_runtime.errors import (
    AgentRuntimeError,
    AgentTimeoutError as RuntimeAgentTimeoutError,
    AgentFailedError as RuntimeAgentFailedError,
    HardAgentError as RuntimeHardAgentError,
    TransientAgentError as RuntimeTransientAgentError,
    UsageLimitError as RuntimeUsageLimitError,
)

_PYCASTLE_SESSION_ROOT = ".pycastle-session"


class PycastleError(AgentRuntimeError):
    pass


class AgentTimeoutError(PycastleError, RuntimeAgentTimeoutError):
    pass


class UsageLimitError(PycastleError, RuntimeUsageLimitError):
    pass


class TransientAgentError(PycastleError, RuntimeTransientAgentError):
    pass


class HardAgentError(PycastleError, RuntimeHardAgentError):
    def __init__(
        self,
        message: str = "",
        status_code: int | None = None,
        service_name: str = "claude",
        classification: str | None = None,
        observations=(),
    ) -> None:
        super().__init__(
            message=message,
            status_code=status_code,
            service_name=service_name,
            classification=classification,
            observations=observations,
        )


class AgentFailedError(PycastleError, RuntimeAgentFailedError):
    def __init__(
        self,
        role_value: str,
        worktree_path,
        namespace: str = "",
        failure_class: str = "",
        service_name: str = "claude",
        provider_session_path: str | None = None,
        session_root: str = _PYCASTLE_SESSION_ROOT,
    ) -> None:
        super().__init__(
            role_value=role_value,
            worktree_path=worktree_path,
            namespace=namespace,
            failure_class=failure_class,
            service_name=service_name,
            provider_session_path=provider_session_path,
            session_root=session_root,
        )


class AgentCredentialFailureError(HardAgentError):
    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        service_name: str,
        classification: str | None = None,
        observations=(),
    ) -> None:
        self.is_operator_actionable = True
        super().__init__(
            message=message,
            status_code=status_code,
            service_name=service_name,
            classification=classification,
            observations=observations,
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
    "PycastleError",
    "SetupPhaseError",
    "TransientAgentError",
    "UsageLimitError",
    "WorktreeError",
    "WorktreeTimeoutError",
]
