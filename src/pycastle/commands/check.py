from __future__ import annotations

import asyncio
import os
import platform
import sys
from pathlib import Path

from ..agents.runner import AgentRunnerProtocol
from ..config import (
    DEFAULT_ENV_FILE,
    Config,
    StageOverride,
    load_config,
    load_credential_env,
    resolve_global_dir,
)
from ..display.status_display import PlainStatusDisplay, StatusDisplay
from ..infrastructure.worktree import transient_worktree
from ..iteration import status_row
from ..main import _configured_service_registry
from ..services import GitService, GithubService, ServiceRegistry
from . import host_check_run as _host_check_run
from .host_check_run import (
    HostCheckFailure,
    HostCheckIssueDeps,
    HostCheckRunOutcome,
    HostCheckRunPassed,
    _run_host_check,
    run_host_check_run,
)

HostCheckFailedError = _host_check_run.HostCheckFailedError


def _surface_current_host_check(status_display: StatusDisplay, name: str) -> None:
    status_display.update_phase("Host Check", name)
    if isinstance(status_display, PlainStatusDisplay):
        status_display.print("Host Check", name)


def _surface_failed_host_checks(
    status_display: StatusDisplay, failures: list[HostCheckFailure]
) -> None:
    for failure in failures:
        status_display.print("Host Check", f"failed {failure.name}", style="error")


def _resolve_github_service(
    repo_root: Path,
    cfg: Config,
    git_svc: GitService,
) -> GithubService:
    resolved = load_credential_env(
        global_dir=resolve_global_dir(None, os.environ),
        local_env_file=DEFAULT_ENV_FILE,
        process_env=os.environ,
    )
    token = resolved.get("GH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GH_TOKEN is required to file host-check issues.")
    remote = git_svc.get_github_remote_repo(repo_root)
    if remote is None:
        raise RuntimeError(
            "Could not resolve GitHub origin repo for host-check issues."
        )
    owner, repo = remote
    return GithubService(f"{owner}/{repo}", token, cfg)


def _resolve_agent_runner(
    cfg: Config,
    git_svc: GitService,
) -> tuple[AgentRunnerProtocol, ServiceRegistry]:
    from ..agents.runner import AgentRunner

    env = load_credential_env(
        global_dir=resolve_global_dir(None, os.environ),
        local_env_file=DEFAULT_ENV_FILE,
        process_env=os.environ,
    )
    service_registry = ServiceRegistry(_configured_service_registry(cfg, env))
    return (
        AgentRunner(env, cfg, git_svc, service_registry=service_registry.services),
        service_registry,
    )


def _resolve_reporter_override(
    cfg: Config, service_registry: ServiceRegistry | None
) -> StageOverride:
    override = cfg.preflight_issue_override
    if service_registry is None:
        return override
    from .. import _time as _time_module

    return service_registry.resolve(override, _time_module.now_local())


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

    async def _run_checks() -> HostCheckRunOutcome:
        async with status_row(
            resolved_status_display,
            "Host Check",
            kind="phase",
            must_close=True,
        ) as row:

            def _issue_deps_factory() -> HostCheckIssueDeps:
                resolved_agent_runner = agent_runner
                resolved_service_registry = service_registry
                if resolved_agent_runner is None:
                    (
                        resolved_agent_runner,
                        resolved_service_registry,
                    ) = _resolve_agent_runner(resolved_cfg, git_svc)
                resolved_github_service = github_service or _resolve_github_service(
                    repo_root, resolved_cfg, git_svc
                )
                return HostCheckIssueDeps(
                    cfg=resolved_cfg,
                    github_svc=resolved_github_service,
                    agent_runner=resolved_agent_runner,
                    status_display=resolved_status_display,
                    reporter_override=_resolve_reporter_override(
                        resolved_cfg, resolved_service_registry
                    ),
                )

            outcome = await run_host_check_run(
                host_checks=resolved_cfg.host_checks,
                git_svc=git_svc,
                repo_root=repo_root,
                issue_deps_factory=_issue_deps_factory,
                on_check_start=lambda name: _surface_current_host_check(
                    resolved_status_display, name
                ),
                on_failures_detected=lambda failures: _surface_failed_host_checks(
                    resolved_status_display, failures
                ),
                run_host_check=_run_host_check,
                transient_worktree_factory=transient_worktree,
            )
            if isinstance(outcome, HostCheckRunPassed):
                row.close("finished")
                return outcome

            row.close(f"failed {outcome.failures[0].name}", shutdown_style="error")
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
