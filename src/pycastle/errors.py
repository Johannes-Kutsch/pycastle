from datetime import datetime
from pathlib import Path


class PycastleError(RuntimeError):
    pass


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


class TransientAgentError(PycastleError):
    def __init__(self, message: str = "", status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class HardAgentError(PycastleError):
    def __init__(
        self,
        message: str = "",
        status_code: int | None = None,
        service_name: str = "claude",
    ) -> None:
        self.status_code = status_code
        self.caller = ""
        self.service_name = service_name or "claude"
        super().__init__(message)


class AgentFailedError(PycastleError):
    def __init__(
        self,
        role_value: str,
        worktree_path: Path,
        namespace: str = "",
        failure_class: str = "",
        service_name: str = "claude",
    ) -> None:
        super().__init__(f"Agent {role_value!r} failed irrecoverably")
        self.role_value = role_value
        self.worktree_path = worktree_path
        self.namespace = namespace
        self.failure_class = failure_class
        self.service_name = service_name or "claude"

    @property
    def session_dir(self) -> str:
        from .agents.output_protocol import AgentRole
        from .session import RoleSession

        return RoleSession(
            self.worktree_path,
            AgentRole(self.role_value),
            self.namespace,
        ).provider_state_relpath(self.service_name)
