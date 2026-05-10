import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..agent_output_protocol import AgentOutput
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import AgentTimeoutError
from ..services import GitService
from ..services import GithubService
from ..status_display import StatusDisplay
from .dispatcher import ImproveMode
from .preflight import PreflightCache, PreflightReady, PreflightResult


class Logger(Protocol):
    def log_error(self, issue: dict, error: Exception) -> None: ...
    def log_agent_output(self, agent_name: str, output: str) -> None: ...


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[dict, Exception]] = []
        self.agent_outputs: list[tuple[str, str]] = []

    def log_error(self, issue: dict, error: Exception) -> None:
        self.errors.append((issue, error))

    def log_agent_output(self, agent_name: str, output: str) -> None:
        self.agent_outputs.append((agent_name, output))


class RecordingStatusDisplay:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def register(
        self,
        caller: str,
        kind: str,
        startup_message: str = "started",
        work_body: str = "",
        initial_phase: str = "Setup",
    ) -> None:
        self.calls.append(("register", caller, kind, startup_message, initial_phase))

    def update_phase(self, name: str, phase: str) -> None:
        self.calls.append(("update_phase", name, phase))

    def reset_idle_timer(self, name: str) -> None:
        self.calls.append(("reset_idle_timer", name))

    def update_tokens(self, name: str, current_tokens: int) -> None:
        self.calls.append(("update_tokens", name, current_tokens))

    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
        shutdown_style: str = "success",
    ) -> None:
        self.calls.append(("remove", caller, shutdown_message, shutdown_style))

    def print(self, caller: str, message: object, style: str | None = None) -> None:
        self.calls.append(("print", caller, message, style))


class FakeAgentRunner:
    """Queue-based test double: pop responses in order, record all calls, or delegate to side_effect."""

    def __init__(
        self,
        responses: list[AgentOutput | BaseException] | None = None,
        *,
        side_effect: Callable[..., Any] | None = None,
        preflight_responses: list[list[tuple[str, str, str]] | BaseException]
        | None = None,
    ) -> None:
        self._responses: list[AgentOutput | BaseException] = list(responses or [])
        self._side_effect = side_effect
        self._preflight_responses: list[list[tuple[str, str, str]] | BaseException] = (
            list(preflight_responses or [])
        )
        self.calls: list[RunRequest] = []
        self.preflight_calls: list[dict] = []

    async def run(self, request: RunRequest) -> AgentOutput:
        try:
            return await self._run(request)
        except AgentTimeoutError as err:
            if not err.role_value:
                err.role_value = request.role.value
                err.worktree_path = request.mount_path
            raise

    async def _run(self, request: RunRequest) -> AgentOutput:
        self.calls.append(request)
        if self._side_effect is not None:
            result = self._side_effect(request)
            if asyncio.iscoroutine(result):
                return await result
            return result
        if not self._responses:
            raise AssertionError(
                f"FakeAgentRunner queue exhausted — unexpected call for agent {request.name!r}"
            )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
        status_display: "StatusDisplay | None" = None,
        work_body: str = "",
    ) -> list[tuple[str, str, str]]:
        call = {
            "name": name,
            "mount_path": mount_path,
            "stage": stage,
            "status_display": status_display,
            "work_body": work_body,
        }
        self.preflight_calls.append(call)
        if not self._preflight_responses:
            raise AssertionError(
                f"FakeAgentRunner preflight queue exhausted — unexpected call for agent {name!r}"
            )
        response = self._preflight_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class StubPreflightCache:
    """Test double: always returns a fixed verdict from get_safe_sha()."""

    def __init__(self, verdict: PreflightResult | None = None) -> None:
        self._verdict: PreflightResult = verdict or PreflightReady(sha="abc123")

    async def get_safe_sha(self, deps: Any) -> PreflightResult:
        return self._verdict


@dataclasses.dataclass
class Deps:
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    agent_runner: AgentRunnerProtocol
    cfg: Config
    logger: Logger
    status_display: StatusDisplay
    improve_mode: ImproveMode = None
    slept_once: bool = False
    improve_dispatched_count: int = 0
    preflight_cache: PreflightCache = dataclasses.field(default_factory=PreflightCache)


def _make_deps(
    repo_root: Path,
    agent_runner: AgentRunnerProtocol,
    *,
    git_svc: GitService | None = None,
    github_svc: GithubService | None = None,
    cfg: Config | None = None,
    logger: Logger | None = None,
    status_display: StatusDisplay | None = None,
    preflight_cache: "PreflightCache | StubPreflightCache | None" = None,
) -> Deps:
    """Factory for building a Deps with sensible test defaults for any unspecified field."""
    from unittest.mock import MagicMock

    return Deps(
        repo_root=repo_root,
        git_svc=git_svc if git_svc is not None else MagicMock(spec=GitService),
        github_svc=github_svc
        if github_svc is not None
        else MagicMock(spec=GithubService),
        agent_runner=agent_runner,
        cfg=cfg if cfg is not None else Config(),
        logger=logger if logger is not None else RecordingLogger(),
        status_display=status_display
        if status_display is not None
        else RecordingStatusDisplay(),
        preflight_cache=preflight_cache
        if preflight_cache is not None
        else StubPreflightCache(),  # type: ignore[arg-type]
    )
