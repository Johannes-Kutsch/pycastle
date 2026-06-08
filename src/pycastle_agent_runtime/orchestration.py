from __future__ import annotations

from pathlib import Path

from pycastle.agents.runner import AgentRunnerProtocol
from pycastle.display.status_display import StatusDisplay
from pycastle.iteration._deps import ImproveMode
from pycastle.services import GitService, GithubService

from .service_registry import ServiceRegistry


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    agent_runner: AgentRunnerProtocol | None = None,
    git_service: GitService | None = None,
    github_service: GithubService | None = None,
    status_display: StatusDisplay | None = None,
    service_registry: ServiceRegistry | None = None,
    improve_mode: ImproveMode = None,
) -> None:
    from pycastle.iteration.orchestrator import run as run_orchestrator

    await run_orchestrator(
        env,
        repo_root,
        agent_runner=agent_runner,
        git_service=git_service,
        github_service=github_service,
        status_display=status_display,
        service_registry=service_registry,
        improve_mode=improve_mode,
    )
