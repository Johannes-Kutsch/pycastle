import builtins
import dataclasses
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..agent_result import PreflightFailure
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


@dataclasses.dataclass
class Deps:
    env: dict[str, str]
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    run_agent: Any
    cfg: Config
    logger: Logger
    status_display: StatusDisplay
