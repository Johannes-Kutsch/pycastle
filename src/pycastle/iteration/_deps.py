import dataclasses
from pathlib import Path
from typing import Literal, Protocol

from pycastle.agents.runner import AgentRunnerProtocol
from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.iteration.preflight import PreflightCache
from pycastle.services import GithubService, GitService, ServiceRegistry

type ImproveMode = Literal["until_sleep", "endless"] | None


class Logger(Protocol):
    def log_error(self, issue: dict, error: Exception) -> None: ...
    def log_internal_error(
        self, label: str, error: Exception, cause: Exception | None = None
    ) -> None: ...
    def log_agent_output(self, agent_name: str, output: str) -> None: ...


@dataclasses.dataclass
class Deps:
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    agent_runner: AgentRunnerProtocol
    cfg: Config
    logger: Logger
    status_display: StatusDisplay
    service_registry: ServiceRegistry | None = None
    improve_mode: ImproveMode = None
    slept_once: bool = False
    improve_dispatched_count: int = 0
    preflight_cache: PreflightCache = dataclasses.field(default_factory=PreflightCache)
