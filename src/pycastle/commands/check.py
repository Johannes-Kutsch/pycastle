from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path
from typing import cast

from .._host_check import (
    HostCheckVerdict,
    HostCheckWorktreeFactory,
    run_host_check_loop,
)
from ..agents.runner import AgentRunnerProtocol
from ..config import Config, load_config
from ..display.status_display import PlainStatusDisplay, StatusDisplay
from ..infrastructure.worktree import transient_worktree
from ..services import GitService, GithubService, ServiceRegistry
from . import host_check_run as _host_check_run
from .host_check_run import (
    HostCheckRunPassed,
    create_host_check_issue_filer,
    resolve_host_check_issue_deps,
    run_host_check_subprocess,
)

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
        resolved_issue_filer = None
        if resolved_cfg.host_checks:
            resolved_issue_filer = _create_host_check_issue_filer(
                cfg=resolved_cfg,
                git_svc=git_svc,
                repo_root=repo_root,
                github_service=github_service,
                agent_runner=agent_runner,
                status_display=resolved_status_display,
                service_registry=service_registry,
            )
        outcome = await run_host_check_loop(
            host_checks=resolved_cfg.host_checks,
            git_svc=git_svc,
            repo_root=repo_root,
            status_display=resolved_status_display,
            run_host_check=run_host_check_subprocess,
            transient_worktree_factory=cast(
                HostCheckWorktreeFactory, transient_worktree
            ),
            file_issue_for_failure=resolved_issue_filer,
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


def _create_host_check_issue_filer(
    *,
    cfg: Config,
    git_svc: GitService,
    repo_root: Path,
    github_service: GithubService | None,
    agent_runner: AgentRunnerProtocol | None,
    status_display: StatusDisplay,
    service_registry: ServiceRegistry | None,
):
    resolved_issue_filer = None

    def get_issue_filer():
        nonlocal resolved_issue_filer
        if resolved_issue_filer is not None:
            return resolved_issue_filer

        issue_deps = resolve_host_check_issue_deps(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=repo_root,
            status_display=status_display,
            github_svc=github_service,
            agent_runner=agent_runner,
            service_registry=service_registry,
        )
        resolved_issue_filer = create_host_check_issue_filer(
            cfg=issue_deps.cfg,
            github_svc=issue_deps.github_svc,
            agent_runner=issue_deps.agent_runner,
            status_display=issue_deps.status_display,
            reporter_override=issue_deps.reporter_override,
        )
        return resolved_issue_filer

    async def file_issue_for_failure(payload, mount_path: Path) -> int:
        return await get_issue_filer()(payload, mount_path)

    return file_issue_for_failure
