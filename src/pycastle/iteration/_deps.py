import dataclasses
from pathlib import Path
from typing import Any, Protocol

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


@dataclasses.dataclass
class Deps:
    env: dict[str, str]
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    run_agent: Any
    cfg: Config
    logger: Logger
