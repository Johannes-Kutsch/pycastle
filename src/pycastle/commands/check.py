from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..agents.output_protocol import AgentRole, IssueOutput
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config, load_config
from ..config import load_env, resolve_global_dir
from ..display.status_display import PlainStatusDisplay, StatusDisplay
from ..infrastructure.worktree import transient_worktree
from ..iteration.preflight import validate_issue_report
from ..main import _configured_service_registry
from ..prompts.pipeline import PromptTemplate
from ..services import GitService, GithubService, ServiceRegistry


@dataclass
class _CheckDeps:
    repo_root: Path
    cfg: Config
    git_svc: GitService


@dataclass(frozen=True)
class _HostCheckFailure:
    name: str
    command: str
    output: str


class HostCheckFailedError(RuntimeError):
    def __init__(self, *, name: str, command: str, output: str) -> None:
        self.name = name
        self.command = command
        self.output = output
        detail = f"\n{output}" if output else ""
        super().__init__(f"Host check {name!r} failed: {command}{detail}")


def _failure_from_exception(
    name: str, command: str, exc: RuntimeError
) -> _HostCheckFailure:
    if isinstance(exc, HostCheckFailedError):
        return _HostCheckFailure(name=exc.name, command=exc.command, output=exc.output)
    text = str(exc)
    if "\n" in text:
        _, output = text.split("\n", 1)
    else:
        output = text
    return _HostCheckFailure(name=name, command=command, output=output.strip())


def _run_host_check(name: str, command: str, cwd: Path) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return
    output = (result.stdout + result.stderr).strip()
    raise HostCheckFailedError(name=name, command=command, output=output)


def _resolve_github_service(
    repo_root: Path,
    cfg: Config,
    git_svc: GitService,
) -> GithubService:
    resolved = load_env(
        global_dir=resolve_global_dir(None, os.environ),
        local_env_file=cfg.env_file,
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

    env = {
        key: value
        for key, value in load_env(
            global_dir=resolve_global_dir(None, os.environ),
            local_env_file=cfg.env_file,
            process_env=os.environ,
        ).items()
        if value
    }
    service_registry = ServiceRegistry(_configured_service_registry(cfg, env))
    return (
        AgentRunner(env, cfg, git_svc, service_registry=service_registry.services),
        service_registry,
    )


async def _file_host_check_issue(
    *,
    failure: _HostCheckFailure,
    mount_path: Path,
    sha: str,
    cfg: Config,
    github_svc: GithubService,
    agent_runner: AgentRunnerProtocol,
    status_display: StatusDisplay,
    service_registry: ServiceRegistry | None,
) -> int:
    override = cfg.preflight_issue_override
    if service_registry is not None:
        from .. import _time as _time_module

        override = service_registry.resolve(override, _time_module.now_local())
    agent_result = await agent_runner.run(
        RunRequest(
            name="Host-Check Reporter",
            template=PromptTemplate.HOST_CHECK_ISSUE,
            mount_path=mount_path,
            role=AgentRole.PREFLIGHT_ISSUE,
            scope_args={
                "HOST_OS": platform.system(),
                "HOST_PLATFORM": platform.platform(),
                "CHECKED_SHA": sha,
                "CHECK_NAME": failure.name,
                "COMMAND": failure.command,
                "OUTPUT": failure.output,
            },
            model=override.model,
            effort=override.effort,
            service=override.service,
            status_display=status_display,
            work_body=f"reporting {failure.name} host-check issue",
        )
    )
    if not isinstance(agent_result, IssueOutput):
        raise RuntimeError(
            f"Host-check issue agent returned unexpected output type: {type(agent_result).__name__}"
        )
    validate_issue_report(
        caller="Host-Check Reporter",
        issue_output=agent_result,
        cfg=cfg,
        github_svc=github_svc,
    )
    return agent_result.number


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

    git_svc.pull_with_merge_fallback(repo_root)
    if not git_svc.is_working_tree_clean(repo_root):
        raise RuntimeError("Working tree must be clean before running host checks.")

    sha = git_svc.get_head_sha(repo_root)
    deps = _CheckDeps(repo_root=repo_root, cfg=resolved_cfg, git_svc=git_svc)

    async def _run_checks() -> bool:
        async with transient_worktree(
            f"host-check-{sha[:7]}", sha=sha, deps=deps
        ) as path:
            failures: list[_HostCheckFailure] = []
            for name, command in resolved_cfg.host_checks:
                try:
                    _run_host_check(name, command, path)
                except RuntimeError as exc:
                    failures.append(_failure_from_exception(name, command, exc))
            if failures:
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
                issue_numbers = []
                for failure in failures:
                    issue_numbers.append(
                        await _file_host_check_issue(
                            failure=failure,
                            mount_path=path,
                            sha=sha,
                            cfg=resolved_cfg,
                            github_svc=resolved_github_service,
                            agent_runner=resolved_agent_runner,
                            status_display=resolved_status_display,
                            service_registry=resolved_service_registry,
                        )
                    )
                joined = ", ".join(f"#{number}" for number in issue_numbers)
                print(f"Host checks filed or updated issues: {joined}")
                sys.stdout.flush()
                return False
            return True

    passed = asyncio.run(_run_checks())
    if passed:
        print(
            "Host checks passed on "
            f"{platform.system()} ({platform.platform()}) at {sha}."
        )
        sys.stdout.flush()
