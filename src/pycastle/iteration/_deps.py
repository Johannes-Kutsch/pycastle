import asyncio
import builtins
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..agent_result import CancellationToken, PreflightFailure
from ..agent_runner import AgentRunnerProtocol
from ..config import Config
from ..git_service import GitService
from ..github_service import GithubService


class Logger(Protocol):
    def log_error(self, issue: dict, error: Exception | PreflightFailure) -> None: ...
    def log_agent_output(self, agent_name: str, output: str) -> None: ...


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[dict, Exception | PreflightFailure]] = []
        self.agent_outputs: list[tuple[str, str]] = []

    def log_error(self, issue: dict, error: Exception | PreflightFailure) -> None:
        self.errors.append((issue, error))

    def log_agent_output(self, agent_name: str, output: str) -> None:
        self.agent_outputs.append((agent_name, output))


@runtime_checkable
class StatusDisplay(Protocol):
    def add_agent(self, name: str, phase: str, log_path: Path) -> None: ...
    def update_phase(self, name: str, phase: str) -> None: ...
    def remove_agent(self, name: str) -> None: ...
    def print(self, message: str) -> None: ...


class NullStatusDisplay:
    def add_agent(self, name: str, phase: str, log_path: Path) -> None:
        pass

    def update_phase(self, name: str, phase: str) -> None:
        pass

    def remove_agent(self, name: str) -> None:
        pass

    def print(self, message: str) -> None:
        builtins.print(message)


class RecordingStatusDisplay:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def add_agent(self, name: str, phase: str, log_path: Path) -> None:
        self.calls.append(("add_agent", name, phase, log_path))

    def update_phase(self, name: str, phase: str) -> None:
        self.calls.append(("update_phase", name, phase))

    def remove_agent(self, name: str) -> None:
        self.calls.append(("remove_agent", name))

    def print(self, message: str) -> None:
        self.calls.append(("print", message))


class FakeAgentRunner:
    """Queue-based test double: pop responses in order, record all calls, or delegate to side_effect."""

    def __init__(
        self,
        responses: list[str | PreflightFailure | BaseException] | None = None,
        *,
        side_effect: Callable[..., Any] | None = None,
    ) -> None:
        self._responses: list[str | PreflightFailure | BaseException] = list(
            responses or []
        )
        self._side_effect = side_effect
        self.calls: list[dict] = []

    async def run(
        self,
        *,
        name: str,
        prompt_file: Path,
        mount_path: Path,
        prompt_args: dict[str, str] | None = None,
        branch: str | None = None,
        sha: str | None = None,
        skip_preflight: bool = False,
        model: str = "",
        effort: str = "",
        stage: str = "",
        token: CancellationToken | None = None,
    ) -> str | PreflightFailure:
        call = {
            "name": name,
            "prompt_file": prompt_file,
            "mount_path": mount_path,
            "prompt_args": prompt_args,
            "branch": branch,
            "sha": sha,
            "skip_preflight": skip_preflight,
            "model": model,
            "effort": effort,
            "stage": stage,
            "token": token,
        }
        self.calls.append(call)
        if self._side_effect is not None:
            result = self._side_effect(**call)
            if asyncio.iscoroutine(result):
                return await result
            return result
        if not self._responses:
            raise AssertionError(
                f"FakeAgentRunner queue exhausted — unexpected call for agent {name!r}"
            )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@dataclasses.dataclass
class Deps:
    env: dict[str, str]
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    agent_runner: AgentRunnerProtocol
    cfg: Config
    logger: Logger
    status_display: StatusDisplay
