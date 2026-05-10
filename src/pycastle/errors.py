from datetime import datetime
from pathlib import Path


class PycastleError(RuntimeError):
    pass


class DockerError(PycastleError):
    pass


class DockerTimeoutError(DockerError, TimeoutError):
    pass


class WorktreeError(PycastleError):
    pass


class WorktreeTimeoutError(WorktreeError, TimeoutError):
    pass


class BranchCollisionError(WorktreeError):
    pass


class AgentTimeoutError(PycastleError, TimeoutError):
    pass


class PreflightFailure(PycastleError):
    def __init__(self, failures: tuple[tuple[str, str, str], ...]) -> None:
        self.failures = failures
        super().__init__(f"Preflight failed: {len(failures)} check(s) failed")


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
    def __init__(self, reset_time: datetime | None = None) -> None:
        self.reset_time = reset_time
        super().__init__(
            f"Usage limit reached (reset_time={reset_time.isoformat() if reset_time else None})"
        )


class AgentFailedError(PycastleError):
    def __init__(
        self, role_value: str, worktree_path: Path, namespace: str = ""
    ) -> None:
        super().__init__(f"Agent {role_value!r} failed irrecoverably")
        self.role_value = role_value
        self.worktree_path = worktree_path
        self.namespace = namespace

    @property
    def session_dir(self) -> str:
        from .agent_output_protocol import (
            AgentRole,
        )  # deferred to break circular import
        from .session_resume import RoleSession

        return (
            RoleSession(Path("/"), AgentRole(self.role_value), self.namespace)
            .claude_config_dir_relpath()
            .rstrip("/")
        )
