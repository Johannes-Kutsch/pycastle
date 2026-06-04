from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, TypeAlias

from .._host_check import (
    HostCheckCommandExecutor,
    HostCheckCommandResult,
    HostCheckFailedError,
    HostCheckFailure,
    HostCheckIssueFiledVerdict,
    HostCheckPassedVerdict,
    HostCheckVerdict,
    HostCheckWorktreeFactory,
)
from ..agents.output_protocol import AgentRole, IssueOutput
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config, StageOverride, load_credential_env
from ..display.status_display import PlainStatusDisplay, StatusDisplay
from ..errors import SetupPhaseError
from ..infrastructure.worktree import transient_worktree
from ..iteration import status_row
from ..iteration.preflight import validate_issue_report
from ..main import _configured_service_registry
from ..prompts.pipeline import PromptTemplate
from ..prompts import scope_args as prompt_scope_args
from ..services import GitService, GithubService, ServiceRegistry


@dataclass(frozen=True)
class HostCheckIssueDeps:
    cfg: Config
    github_svc: GithubService
    agent_runner: AgentRunnerProtocol
    status_display: StatusDisplay
    reporter_override: StageOverride | None = None


HostCheckRunPassed = HostCheckPassedVerdict
HostCheckRunFailed = HostCheckIssueFiledVerdict
HostCheckRunOutcome: TypeAlias = HostCheckVerdict


@dataclass
class _CheckDeps:
    repo_root: Path
    git_svc: GitService


def _resolve_github_service(
    repo_root: Path,
    cfg: Config,
    git_svc: GitService,
) -> GithubService:
    resolved = load_credential_env()
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

    env = load_credential_env()
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


def resolve_host_check_issue_deps(
    *,
    cfg: Config,
    git_svc: GitService,
    repo_root: Path,
    status_display: StatusDisplay,
    github_svc: GithubService | None = None,
    agent_runner: AgentRunnerProtocol | None = None,
    service_registry: ServiceRegistry | None = None,
) -> HostCheckIssueDeps:
    resolved_agent_runner = agent_runner
    resolved_service_registry = service_registry
    if resolved_agent_runner is None:
        (
            resolved_agent_runner,
            resolved_service_registry,
        ) = _resolve_agent_runner(cfg, git_svc)
    resolved_github_svc = github_svc or _resolve_github_service(repo_root, cfg, git_svc)
    return HostCheckIssueDeps(
        cfg=cfg,
        github_svc=resolved_github_svc,
        agent_runner=resolved_agent_runner,
        status_display=status_display,
        reporter_override=_resolve_reporter_override(cfg, resolved_service_registry),
    )


def _surface_current_host_check(status_display: StatusDisplay, name: str) -> None:
    status_display.update_phase("Host Check", name)
    if isinstance(status_display, PlainStatusDisplay):
        status_display.print("Host Check", name)


def _surface_failed_host_checks(
    status_display: StatusDisplay, failures: list[HostCheckFailure]
) -> None:
    for failure in failures:
        status_display.print("Host Check", f"failed {failure.name}", style="error")


def _preserve_host_check_context(
    exc: SetupPhaseError, failure: HostCheckFailure
) -> SetupPhaseError:
    return SetupPhaseError(
        exc.phase,
        "Host-Check Reporter setup failed while reporting "
        f"failed host check {failure.name!r}: {exc}",
        command=exc.command or failure.command,
        output=exc.output or failure.output,
    )


def _run_host_check(name: str, command: str, cwd: Path) -> HostCheckCommandResult:
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        capture_output=True,
    )
    return HostCheckCommandResult(
        name=name,
        command=command,
        returncode=result.returncode,
        output=(result.stdout + result.stderr).strip(),
    )


def _failure_from_command_result(
    command_result: HostCheckCommandResult,
) -> HostCheckFailure:
    return HostCheckFailure(
        name=command_result.name,
        command=command_result.command,
        output=command_result.output.strip(),
    )


def _failure_from_exception(
    name: str, command: str, exc: RuntimeError
) -> HostCheckFailure:
    if isinstance(exc, HostCheckFailedError):
        return HostCheckFailure(
            name=exc.name,
            command=exc.command,
            output=exc.output.strip(),
        )

    text = str(exc)
    prefix = f"Host check {name!r} failed: {command}"
    output = text.removeprefix(prefix).lstrip("\n")
    return HostCheckFailure(name=name, command=command, output=output.strip())


def prepare_host_check_run(
    *, git_svc: GitService, repo_root: Path | None = None
) -> str:
    resolved_repo_root = repo_root or Path(".").resolve()
    git_svc.pull_with_merge_fallback(resolved_repo_root)
    if not git_svc.is_working_tree_clean(resolved_repo_root):
        raise RuntimeError("Working tree must be clean before running host checks.")
    return git_svc.get_head_sha(resolved_repo_root)


async def _file_host_check_issue(
    *,
    failure: HostCheckFailure,
    mount_path: Path,
    sha: str,
    cfg: Config,
    github_svc: GithubService,
    agent_runner: AgentRunnerProtocol,
    status_display: StatusDisplay,
    reporter_override: StageOverride | None,
) -> int:
    override = reporter_override or cfg.preflight_issue_override
    agent_result = await agent_runner.run(
        RunRequest(
            name="Host-Check Reporter",
            template=PromptTemplate.HOST_CHECK_ISSUE,
            mount_path=mount_path,
            role=AgentRole.PREFLIGHT_ISSUE,
            scope_args=prompt_scope_args.build_host_check_scope_args(
                checked_sha=sha,
                check_name=failure.name,
                command=failure.command,
                output=failure.output,
            ),
            model=override.model,
            effort=override.effort,
            service=override.service,
            status_display=status_display,
            work_body=f"reporting {failure.name} host-check issue",
        )
    )
    if not isinstance(agent_result, IssueOutput):
        raise RuntimeError(
            "Host-check issue agent returned unexpected output type: "
            f"{type(agent_result).__name__}"
        )
    validate_issue_report(
        caller="Host-Check Reporter",
        issue_output=agent_result,
        cfg=cfg,
        github_svc=github_svc,
    )
    return agent_result.number


async def run_host_check_command(
    *,
    cfg: Config,
    git_svc: GitService,
    repo_root: Path | None = None,
    github_svc: GithubService | None = None,
    agent_runner: AgentRunnerProtocol | None = None,
    status_display: StatusDisplay | None = None,
    service_registry: ServiceRegistry | None = None,
) -> HostCheckRunOutcome:
    resolved_repo_root = repo_root or Path(".").resolve()
    resolved_status_display = status_display or PlainStatusDisplay()
    return await run_host_check_run(
        host_checks=cfg.host_checks,
        git_svc=git_svc,
        repo_root=resolved_repo_root,
        status_display=resolved_status_display,
        issue_deps_factory=lambda: resolve_host_check_issue_deps(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=resolved_repo_root,
            status_display=resolved_status_display,
            github_svc=github_svc,
            agent_runner=agent_runner,
            service_registry=service_registry,
        ),
    )


async def run_host_check_run(
    *,
    host_checks: tuple[tuple[str, str], ...],
    git_svc: GitService,
    repo_root: Path | None = None,
    cfg: Config | None = None,
    github_svc: GithubService | None = None,
    agent_runner: AgentRunnerProtocol | None = None,
    status_display: StatusDisplay | None = None,
    reporter_override: StageOverride | None = None,
    issue_deps_factory: Callable[[], HostCheckIssueDeps] | None = None,
    on_check_start: Callable[[str], None] | None = None,
    on_failures_detected: Callable[[list[HostCheckFailure]], None] | None = None,
    run_host_check: HostCheckCommandExecutor | None = None,
    transient_worktree_factory: HostCheckWorktreeFactory | None = None,
) -> HostCheckRunOutcome:
    resolved_repo_root = repo_root or Path(".").resolve()
    execute_host_check = run_host_check or _run_host_check
    create_transient_worktree = transient_worktree_factory or transient_worktree

    async def _run_checks() -> HostCheckRunOutcome:
        checked_sha = prepare_host_check_run(
            git_svc=git_svc, repo_root=resolved_repo_root
        )
        deps = _CheckDeps(repo_root=resolved_repo_root, git_svc=git_svc)
        async with create_transient_worktree(
            f"host-check-{checked_sha[:7]}", sha=checked_sha, deps=deps
        ) as path:
            failures: list[HostCheckFailure] = []
            for name, command in host_checks:
                if status_display is not None:
                    _surface_current_host_check(status_display, name)
                if on_check_start is not None:
                    on_check_start(name)
                try:
                    command_result = execute_host_check(name, command, path)
                except RuntimeError as exc:
                    failures.append(_failure_from_exception(name, command, exc))
                    continue
                if command_result.returncode != 0:
                    failures.append(_failure_from_command_result(command_result))
            if failures:
                if status_display is not None:
                    _surface_failed_host_checks(status_display, failures)
                if on_failures_detected is not None:
                    on_failures_detected(failures)
                issue_numbers: tuple[int, ...] = ()
                resolved_cfg = cfg
                resolved_github_svc = github_svc
                resolved_agent_runner = agent_runner
                resolved_status_display = status_display
                resolved_reporter_override = reporter_override
                if issue_deps_factory is not None and (
                    resolved_cfg is None
                    or resolved_github_svc is None
                    or resolved_agent_runner is None
                    or resolved_status_display is None
                ):
                    issue_deps = issue_deps_factory()
                    resolved_cfg = issue_deps.cfg
                    resolved_github_svc = issue_deps.github_svc
                    resolved_agent_runner = issue_deps.agent_runner
                    resolved_status_display = issue_deps.status_display
                    resolved_reporter_override = issue_deps.reporter_override
                if (
                    resolved_cfg is not None
                    and resolved_github_svc is not None
                    and resolved_agent_runner is not None
                    and resolved_status_display is not None
                ):
                    filed_issue_numbers: list[int] = []
                    for failure in failures:
                        try:
                            filed_issue_numbers.append(
                                await _file_host_check_issue(
                                    failure=failure,
                                    mount_path=path,
                                    sha=checked_sha,
                                    cfg=resolved_cfg,
                                    github_svc=resolved_github_svc,
                                    agent_runner=resolved_agent_runner,
                                    status_display=resolved_status_display,
                                    reporter_override=resolved_reporter_override,
                                )
                            )
                        except SetupPhaseError as exc:
                            raise _preserve_host_check_context(exc, failure) from exc
                    issue_numbers = tuple(filed_issue_numbers)
                return HostCheckRunFailed(
                    checked_sha=checked_sha,
                    failures=tuple(failures),
                    issue_numbers=issue_numbers,
                )
            return HostCheckRunPassed(checked_sha=checked_sha)

    if status_display is None:
        return await _run_checks()

    async with status_row(
        status_display,
        "Host Check",
        kind="phase",
        must_close=True,
    ) as row:
        outcome = await _run_checks()
        if isinstance(outcome, HostCheckRunPassed):
            row.close("finished")
            return outcome
        row.close(f"failed {outcome.failures[0].name}", shutdown_style="error")
        return outcome
