from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable, TypeAlias, cast

from .. import _host_check as _host_check_module
from .._host_check import (
    HostCheckCommandExecutor,
    HostCheckCommandResult,
    HostCheckFailure,
    HostCheckIssuePayload,
    HostCheckIssueFiledVerdict,
    HostCheckIssueFiler,
    HostCheckPassedVerdict,
    HostCheckVerdict,
    HostCheckWorktreeFactory,
    prepare_host_check_loop,
    run_host_check_loop,
)
from ..agents.output_protocol import AgentRole, IssueOutput
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config, StageOverride, load_credential_env
from ..display.status_display import PlainStatusDisplay, StatusDisplay
from ..diagnostic_issue_report_validation import validate_diagnostic_issue_report
from ..errors import SetupPhaseError
from ..infrastructure.worktree import detached_transient_worktree
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..prompts import scope_args as prompt_scope_args
from ..run_startup_preparation import (
    RunStartupImproveModeFlagFacts,
    prepare_run_startup,
)
from ..services import GitService, GithubService, ServiceRegistry


@dataclass(frozen=True)
class HostCheckIssueDeps:
    cfg: Config
    github_svc: GithubService
    agent_runner: AgentRunnerProtocol
    status_display: StatusDisplay
    reporter_override: StageOverride | None = None


HostCheckFailedError = _host_check_module.HostCheckFailedError
HostCheckRunPassed = HostCheckPassedVerdict
HostCheckRunFailed = HostCheckIssueFiledVerdict
HostCheckRunOutcome: TypeAlias = HostCheckVerdict


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
    startup = prepare_run_startup(
        cfg,
        env,
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )
    configured_services = startup.configured_provider_adapters
    service_registry = startup.runtime_registry
    return (
        AgentRunner(env, cfg, git_svc, service_registry=configured_services),
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


def run_host_check_subprocess(
    name: str, command: str, cwd: Path
) -> HostCheckCommandResult:
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
        output=result.stdout + result.stderr,
    )


_run_host_check = run_host_check_subprocess


def _create_host_check_transient_worktree(
    name: str, *, sha: str, deps
) -> AbstractAsyncContextManager[Path]:
    return detached_transient_worktree(name, sha=sha, deps=deps)


transient_worktree = _create_host_check_transient_worktree


def prepare_host_check_run(
    *, git_svc: GitService, repo_root: Path | None = None
) -> str:
    return prepare_host_check_loop(git_svc=git_svc, repo_root=repo_root)


def _validate_host_check_issue_report(
    *,
    issue_output: IssueOutput,
    cfg: Config,
    github_svc: GithubService,
) -> None:
    validate_diagnostic_issue_report(
        caller="Host-Check Reporter",
        issue_output=issue_output,
        cfg=cfg,
        filed_issue_reader=github_svc,
    )


async def _file_host_check_issue(
    *,
    payload: HostCheckIssuePayload,
    mount_path: Path,
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
            prompt=build_prompt_invocation(
                PromptTemplate.HOST_CHECK_ISSUE,
                prompt_scope_args.build_host_check_scope_args(
                    checked_sha=payload.checked_sha,
                    check_name=payload.check_name,
                    command=payload.command,
                    output=payload.output,
                    host_os=payload.host_os,
                    host_platform=payload.host_platform,
                ),
            ),
            mount_path=mount_path,
            role=AgentRole.PREFLIGHT_ISSUE,
            model=override.model,
            effort=override.effort,
            service=override.service,
            status_display=status_display,
            work_body=f"reporting {payload.check_name} host-check issue",
        )
    )
    if not isinstance(agent_result, IssueOutput):
        raise RuntimeError(
            "Host-Check Reporter returned non-issue output: "
            f"{type(agent_result).__name__}"
        )
    _validate_host_check_issue_report(
        issue_output=agent_result,
        cfg=cfg,
        github_svc=github_svc,
    )
    return agent_result.number


def create_host_check_issue_filer(
    *,
    cfg: Config,
    github_svc: GithubService,
    agent_runner: AgentRunnerProtocol,
    status_display: StatusDisplay,
    reporter_override: StageOverride | None,
) -> HostCheckIssueFiler:
    async def file_issue_for_failure(
        payload: HostCheckIssuePayload, mount_path: Path
    ) -> int:
        try:
            return await _file_host_check_issue(
                payload=payload,
                mount_path=mount_path,
                cfg=cfg,
                github_svc=github_svc,
                agent_runner=agent_runner,
                status_display=status_display,
                reporter_override=reporter_override,
            )
        except SetupPhaseError as exc:
            raise _preserve_host_check_context(
                exc,
                HostCheckFailure(
                    name=payload.check_name,
                    command=payload.command,
                    output=payload.output,
                ),
            ) from exc

    return file_issue_for_failure


async def run_host_check_command(
    *,
    cfg: Config,
    git_svc: GitService,
    repo_root: Path | None = None,
    github_svc: GithubService | None = None,
    agent_runner: AgentRunnerProtocol | None = None,
    status_display: StatusDisplay | None = None,
    service_registry: ServiceRegistry | None = None,
    run_host_check: HostCheckCommandExecutor | None = None,
    transient_worktree_factory: HostCheckWorktreeFactory | None = None,
) -> HostCheckRunOutcome:
    resolved_repo_root = repo_root or Path(".").resolve()
    resolved_status_display = status_display or PlainStatusDisplay()
    resolved_reporter_override: StageOverride | None = None
    if github_svc is not None and agent_runner is not None:
        resolved_reporter_override = _resolve_reporter_override(cfg, service_registry)
    return await run_host_check_run(
        host_checks=cfg.host_checks,
        git_svc=git_svc,
        repo_root=resolved_repo_root,
        cfg=cfg,
        github_svc=github_svc,
        agent_runner=agent_runner,
        status_display=resolved_status_display,
        reporter_override=resolved_reporter_override,
        issue_deps_factory=lambda: resolve_host_check_issue_deps(
            cfg=cfg,
            git_svc=git_svc,
            repo_root=resolved_repo_root,
            status_display=resolved_status_display,
            github_svc=github_svc,
            agent_runner=agent_runner,
            service_registry=service_registry,
        ),
        run_host_check=run_host_check,
        transient_worktree_factory=transient_worktree_factory,
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
    create_transient_worktree = cast(
        HostCheckWorktreeFactory,
        transient_worktree_factory or transient_worktree,
    )
    file_issue_for_failure: HostCheckIssueFiler | None = None
    if (
        cfg is not None
        and github_svc is not None
        and agent_runner is not None
        and status_display is not None
    ):
        file_issue_for_failure = create_host_check_issue_filer(
            cfg=cfg,
            github_svc=github_svc,
            agent_runner=agent_runner,
            status_display=status_display,
            reporter_override=reporter_override,
        )
    elif issue_deps_factory is not None:
        resolved_issue_deps: HostCheckIssueDeps | None = None

        def get_issue_deps() -> HostCheckIssueDeps:
            nonlocal resolved_issue_deps
            if resolved_issue_deps is None:
                resolved_issue_deps = issue_deps_factory()
            return resolved_issue_deps

        async def file_issue_for_failure(
            payload: HostCheckIssuePayload, mount_path: Path
        ) -> int:
            issue_deps = get_issue_deps()
            return await create_host_check_issue_filer(
                cfg=issue_deps.cfg,
                github_svc=issue_deps.github_svc,
                agent_runner=issue_deps.agent_runner,
                status_display=issue_deps.status_display,
                reporter_override=issue_deps.reporter_override,
            )(payload, mount_path)

    return await run_host_check_loop(
        host_checks=host_checks,
        git_svc=git_svc,
        repo_root=resolved_repo_root,
        status_display=status_display,
        on_check_start=on_check_start,
        on_failures_detected=on_failures_detected,
        run_host_check=execute_host_check,
        transient_worktree_factory=create_transient_worktree,
        file_issue_for_failure=file_issue_for_failure,
    )
