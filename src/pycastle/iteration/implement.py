import asyncio
import contextlib
import dataclasses
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..agents.output_protocol import AgentRole, CommitMessageOutput
from ..agents.result import CancellationToken
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import (
    AgentFailedError,
    BranchCollisionError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..issue_readiness import require_ready_slice_outcome_for_issue
from ..session import RoleSession, is_stage_done_for
from ..display.status_display import StatusDisplay
from ..services import GitService, GithubService
from ..infrastructure.worktree import (
    DurableIssueWorktreeIntent,
    durable_issue_worktree,
    issue_branch,
    worktree_identity,
)
from ._deps import Logger
from .implement_issue_plan import IssueRoleStepPlan, plan_issue_execution_from_worktree

if TYPE_CHECKING:
    from ..services import ServiceRegistry


class _ImplementDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    git_svc: GitService
    github_svc: GithubService
    repo_root: Path
    logger: Logger
    service_registry: "ServiceRegistry | None"


def branch_for(issue_number: int) -> str:
    return issue_branch(issue_number)


def _resolve_slice(issue: dict, cfg: Config) -> tuple[str, PromptTemplate]:
    ready = require_ready_slice_outcome_for_issue(issue, cfg)
    return (ready.display_name, ready.template)


def pick_implement_template(issue: dict, cfg: Config) -> PromptTemplate:
    return _resolve_slice(issue, cfg)[1]


def pick_slice_mode(issue: dict, cfg: Config) -> str:
    return _resolve_slice(issue, cfg)[0]


@dataclasses.dataclass
class ImplementResult:
    completed: list[dict]
    errors: list[tuple[dict, Exception]]
    usage_limit_hit: bool = False
    usage_limit_reset_time: datetime | None = None
    usage_limit_provider: str | None = None
    usage_limit_raw_message: str | None = None
    usage_limit_account_label: str | None = None
    usage_limit_is_permanent: bool = False


def _request_name(step: IssueRoleStepPlan, issue_number: int) -> str:
    prefix = "Implement" if step.role_name == "implementer" else "Review"
    return f"{prefix} Agent #{issue_number}"


def _build_run_request(
    *,
    issue: dict,
    step: IssueRoleStepPlan,
    mount_path: Path,
    status_display: StatusDisplay,
    token: CancellationToken,
) -> RunRequest:
    return RunRequest(
        name=_request_name(step, issue["number"]),
        prompt=build_prompt_invocation(step.prompt_template, step.prompt_scope_args),
        mount_path=mount_path,
        role=step.role,
        model=step.model,
        effort=step.effort,
        service=step.service,
        stage=step.stage,
        status_display=status_display,
        issue_title=issue["title"],
        work_body=step.work_body,
        token=token,
    )


def _planned_commit_subject(
    step: IssueRoleStepPlan, issue: dict, message: str | None
) -> str:
    fallback = step.commit_fallback_subject
    if fallback is None:
        prefix = "Implement" if step.role is AgentRole.IMPLEMENTER else "Review"
        if message is None:
            return f"{prefix} #{issue['number']} - {issue['title']}"
        return f"{prefix} #{issue['number']} - {message}"
    if message is None:
        return fallback.fallback_subject
    return f"{fallback.commit_prefix}{message}"


async def run_issue(
    issue: dict,
    deps: _ImplementDeps,
    sha: str | None,
    semaphore: asyncio.Semaphore | None = None,
    *,
    worktree_semaphore: asyncio.Semaphore | None = None,
    token: CancellationToken | None = None,
    branch_locks: dict[str, asyncio.Lock] | None = None,
    on_started: Callable[[str], None] | None = None,
) -> dict:
    _branch = branch_for(issue["number"])
    _token = token if token is not None else CancellationToken()
    _resolve_slice(issue, deps.cfg)

    _implement_started = False
    _review_started = False

    async def _bounded_run_agent(request: RunRequest) -> Any:
        nonlocal _implement_started, _review_started
        async with semaphore or contextlib.nullcontext():
            if on_started is not None:
                if request.role == AgentRole.IMPLEMENTER and not _implement_started:
                    on_started("implement")
                    _implement_started = True
                elif request.role == AgentRole.REVIEWER and not _review_started:
                    on_started("review")
                    _review_started = True
            return await deps.agent_runner.run(request)

    lock: asyncio.Lock | None = None
    if branch_locks is not None:
        if _branch not in branch_locks:
            branch_locks[_branch] = asyncio.Lock()
        lock = branch_locks[_branch]
        if lock.locked():
            raise BranchCollisionError(
                f"Branch {_branch!r} already has an agent running"
            )
        await lock.acquire()

    try:
        _worktree = worktree_identity(_branch, deps.repo_root)
        _wt_name = _worktree.name
        _wt_path = _worktree.path

        issue_plan = plan_issue_execution_from_worktree(
            issue=issue,
            deps=deps,
            sha=sha,
            worktree_path=_wt_path,
            implement_mount_path=_wt_path,
            review_mount_path=_wt_path,
        )

        if issue_plan.issue_outcome == "complete":
            return issue

        runnable_roles = {step.role_name for step in issue_plan.run_steps}
        planned_steps = {step.role_name: step for step in issue_plan.steps}

        if "implementer" in runnable_roles:
            async with (
                worktree_semaphore or contextlib.nullcontext(),
                durable_issue_worktree(
                    issue["number"],
                    intent=DurableIssueWorktreeIntent.IMPLEMENTER,
                    deps=deps,
                    planner_sha=sha,
                ) as impl_mount_path,
            ):
                implementer_step = planned_steps["implementer"]
                if implementer_step.mount_setup_failure is not None:
                    raise SetupPhaseError(
                        implementer_step.mount_setup_failure.role_value,
                        implementer_step.mount_setup_failure.error_message,
                    )
                result = await _bounded_run_agent(
                    _build_run_request(
                        issue=issue,
                        step=implementer_step,
                        mount_path=impl_mount_path,
                        status_display=deps.status_display,
                        token=_token,
                    )
                )
                if isinstance(result, CommitMessageOutput):
                    deps.git_svc.commit(
                        impl_mount_path,
                        deps.repo_root,
                        _planned_commit_subject(
                            implementer_step, issue, result.message
                        ),
                    )
                    RoleSession(
                        impl_mount_path, AgentRole.IMPLEMENTER
                    ).clear_provider_state_and_signal_completion()

        if "reviewer" in runnable_roles:
            async with (
                worktree_semaphore or contextlib.nullcontext(),
                durable_issue_worktree(
                    issue["number"],
                    intent=DurableIssueWorktreeIntent.REVIEWER,
                    deps=deps,
                ) as review_mount_path,
            ):
                reviewer_step = planned_steps["reviewer"]
                if reviewer_step.mount_setup_failure is not None:
                    raise SetupPhaseError(
                        reviewer_step.mount_setup_failure.role_value,
                        reviewer_step.mount_setup_failure.error_message,
                    )
                review_result = await _bounded_run_agent(
                    _build_run_request(
                        issue=issue,
                        step=reviewer_step,
                        mount_path=review_mount_path,
                        status_display=deps.status_display,
                        token=_token,
                    )
                )
                if isinstance(review_result, CommitMessageOutput):
                    deps.git_svc.commit(
                        review_mount_path,
                        deps.repo_root,
                        _planned_commit_subject(
                            reviewer_step, issue, review_result.message
                        ),
                    )
                    RoleSession(
                        review_mount_path, AgentRole.REVIEWER
                    ).clear_provider_state_and_signal_completion()
    finally:
        if lock is not None and lock.locked():
            lock.release()

    return issue


async def implement_phase(
    issues: list[dict],
    deps: _ImplementDeps,
    sha: str | None,
    *,
    token: CancellationToken | None = None,
) -> ImplementResult:
    _token = token if token is not None else CancellationToken()
    for issue in issues:
        _resolve_slice(issue, deps.cfg)
    semaphore = asyncio.Semaphore(deps.cfg.max_parallel)
    worktree_semaphore = asyncio.Semaphore(deps.cfg.max_parallel + 1)
    branch_locks: dict[str, asyncio.Lock] = {}
    total = len(issues)

    def _stage_done_count(role: AgentRole) -> int:
        return sum(
            is_stage_done_for(
                worktree_identity(branch_for(issue["number"]), deps.repo_root).path,
                role,
            )
            for issue in issues
        )

    implement_started = _stage_done_count(AgentRole.IMPLEMENTER)
    review_started = _stage_done_count(AgentRole.REVIEWER)

    def _progress_text() -> str:
        parts = [f"started implement Agents for {implement_started}/{total} issues"]
        parts.append(f"started review Agents for {review_started}/{total} issues")
        return "Running: " + " · ".join(parts)

    deps.status_display.update_phase("Implement", _progress_text())

    def _on_started(role: str) -> None:
        nonlocal implement_started, review_started
        if role == "implement":
            implement_started += 1
        else:
            review_started += 1
        deps.status_display.update_phase("Implement", _progress_text())

    results = await asyncio.gather(
        *[
            run_issue(
                issue,
                deps,
                sha,
                semaphore,
                worktree_semaphore=worktree_semaphore,
                token=_token,
                branch_locks=branch_locks,
                on_started=_on_started,
            )
            for issue in issues
        ],
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, AgentFailedError):
            raise result
    for result in results:
        if isinstance(result, HardAgentError):
            raise result
    for result in results:
        if isinstance(result, TransientAgentError):
            raise result
    usage_limit_errors = [r for r in results if isinstance(r, UsageLimitError)]
    usage_limit_hit = bool(usage_limit_errors)
    usage_limit_reset_time = next(
        (e.reset_time for e in usage_limit_errors if e.reset_time is not None),
        None,
    )
    first_usage_limit_error = usage_limit_errors[0] if usage_limit_errors else None
    completed: list[dict] = []
    errors: list[tuple[dict, Exception]] = []
    for issue, result in zip(issues, results):
        if isinstance(result, UsageLimitError):
            continue
        elif isinstance(result, Exception):
            deps.logger.log_error(issue, result)
            errors.append((issue, result))
        elif isinstance(result, dict):
            completed.append(issue)
    return ImplementResult(
        completed=completed,
        errors=errors,
        usage_limit_hit=usage_limit_hit,
        usage_limit_reset_time=usage_limit_reset_time,
        usage_limit_provider=(
            first_usage_limit_error.provider if first_usage_limit_error else None
        ),
        usage_limit_raw_message=(
            first_usage_limit_error.raw_message if first_usage_limit_error else None
        ),
        usage_limit_account_label=(
            first_usage_limit_error.account_label if first_usage_limit_error else None
        ),
        usage_limit_is_permanent=(
            first_usage_limit_error.is_permanent if first_usage_limit_error else False
        ),
    )
