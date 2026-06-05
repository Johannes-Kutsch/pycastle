from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path
from typing import cast

from .. import _time as _time_module
from .._host_check import (
    HostCheckVerdict,
    HostCheckWorktreeFactory,
    run_host_check_loop,
)
from ..agents.runner import AgentRunnerProtocol
from ..config import Config, load_config, load_credential_env
from ..display.status_display import PlainStatusDisplay, StatusDisplay
from ..infrastructure.worktree import transient_worktree
from ..main import _configured_service_registry
from ..services import GitService, GithubService, ServiceRegistry
from . import host_check_run as _host_check_run
from .host_check_run import (
    HostCheckRunPassed,
    create_host_check_issue_filer,
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

        env = load_credential_env()
        resolved_service_registry = service_registry or ServiceRegistry(
            _configured_service_registry(cfg, env)
        )
        resolved_agent_runner = agent_runner
        if resolved_agent_runner is None:
            from ..agents.runner import AgentRunner

            resolved_agent_runner = AgentRunner(
                env,
                cfg,
                git_svc,
                service_registry=resolved_service_registry.services,
            )

        resolved_github_service = github_service
        if resolved_github_service is None:
            token = env.get("GH_TOKEN", "").strip()
            if not token:
                raise RuntimeError("GH_TOKEN is required to file host-check issues.")
            remote = git_svc.get_github_remote_repo(repo_root)
            if remote is None:
                raise RuntimeError(
                    "Could not resolve GitHub origin repo for host-check issues."
                )
            owner, repo = remote
            resolved_github_service = GithubService(f"{owner}/{repo}", token, cfg)

        reporter_override = cfg.preflight_issue_override
        if resolved_service_registry is not None:
            reporter_override = resolved_service_registry.resolve(
                reporter_override, _time_module.now_local()
            )
        resolved_issue_filer = create_host_check_issue_filer(
            cfg=cfg,
            github_svc=resolved_github_service,
            agent_runner=resolved_agent_runner,
            status_display=status_display,
            reporter_override=reporter_override,
        )
        return resolved_issue_filer

    async def file_issue_for_failure(payload, mount_path: Path) -> int:
        return await get_issue_filer()(payload, mount_path)

    return file_issue_for_failure
