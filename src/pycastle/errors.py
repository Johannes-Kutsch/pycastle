from pycastle_agent_runtime.errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    HardAgentError,
    PycastleError,
    TransientAgentError,
    UsageLimitError,
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
