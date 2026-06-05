from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path

from .._host_check import HostCheckVerdict
from ..agents.runner import AgentRunnerProtocol
from ..config import Config, load_config
from ..display.status_display import PlainStatusDisplay, StatusDisplay
from ..services import GitService, GithubService, ServiceRegistry
from . import host_check_run as _host_check_run
from .host_check_run import HostCheckRunPassed, run_host_check_command

HostCheckFailedError = _host_check_run.HostCheckFailedError


def main(
    *,
    cfg: Config | None = None,
    git_service: GitService | None = None,
    github_service: GithubService | None = None,
    agent_runner: AgentRunnerProtocol | None = None,
    status_display: StatusDisplay | None = None,
    service_registry: ServiceRegistry | None = None,
) -> None:
    resolved_cfg = cfg or load_config()
    repo_root = Path(".").resolve()
    git_svc = git_service or GitService(resolved_cfg)
    resolved_status_display = status_display or PlainStatusDisplay()

    async def _run_checks() -> HostCheckVerdict:
        outcome = await run_host_check_command(
            cfg=resolved_cfg,
            git_svc=git_svc,
            repo_root=repo_root,
            github_svc=github_service,
            agent_runner=agent_runner,
            status_display=resolved_status_display,
            service_registry=service_registry,
        )
        if isinstance(outcome, HostCheckRunPassed):
            return outcome

        joined = ", ".join(f"#{number}" for number in outcome.issue_numbers)
        print(f"Host checks filed or updated issues: {joined}")
        sys.stdout.flush()
        return outcome

    outcome = asyncio.run(_run_checks())
    if isinstance(outcome, HostCheckRunPassed):
        print(
            "Host checks passed on "
            f"{platform.system()} ({platform.platform()}) at {outcome.checked_sha}."
        )
        sys.stdout.flush()
