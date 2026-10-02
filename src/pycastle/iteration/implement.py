import asyncio
import contextlib
import dataclasses
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pycastle.agents.output_protocol import (
    AgentRole,
    AgentSuccessOutput,
    CommitMessageOutput,
)
from pycastle.agents.runner import AgentRunnerProtocol, RunRequest
from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.errors import (
    BranchCollisionError,
    SetupPhaseError,
)
from pycastle.execution_contracts import CancellationToken
from pycastle.infrastructure.worktree import (
    DurableIssueWorktreeIntent,
    durable_issue_worktree,
    issue_branch,
    worktree_identity,
)
from pycastle.issue_readiness import require_ready_slice_outcome_for_issue
from pycastle.iteration._deps import Logger
from pycastle.iteration.implement_issue_plan import (
    IssueRoleStepPlan,
    plan_issue_execution_from_worktree,
)
from pycastle.prompts.dispatch import build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import GithubService, GitService
from pycastle.session import RoleSession, is_stage_done_for

if TYPE_CHECKING:
    from pycastle.services import ServiceRegistry


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


async def _acquire_branch_lock(
    branch_locks: dict[str, asyncio.Lock] | None,
    branch: str,
) -> asyncio.Lock | None:
    if branch_locks is None:
        return None
    if branch not in branch_locks:
        branch_locks[branch] = asyncio.Lock()
    lock = branch_locks[branch]
    if lock.locked():
        raise BranchCollisionError(f"Branch {branch!r} already has an agent running")
    await lock.acquire()
    return lock


def _check_setup_failure(step: IssueRoleStepPlan) -> None:
    if step.mount_setup_failure is not None:
        raise SetupPhaseError(
            step.mount_setup_failure.role_value,
            step.mount_setup_failure.error_message,
        )


async def _execute_role_step(
    *,
    issue: dict,
    step: IssueRoleStepPlan,
    runner: "_BoundedAgentRunner",
    deps: _ImplementDeps,
    token: CancellationToken,
    worktree_semaphore: asyncio.Semaphore | None,
) -> None:
    intent = (
        DurableIssueWorktreeIntent.IMPLEMENTER
        if step.role is AgentRole.IMPLEMENTER
        else DurableIssueWorktreeIntent.REVIEWER
    )
    async with (
        worktree_semaphore or contextlib.nullcontext(),
        durable_issue_worktree(
            issue["number"],
            intent=intent,
            deps=deps,
            planner_sha=step.planner_sha,
            operating_branch=deps.cfg.operating_branch,
        ) as mount_path,
    ):
        _check_setup_failure(step)
        result = await runner.run(
            _build_run_request(
                issue=issue,
                step=step,
                mount_path=mount_path,
                status_display=deps.status_display,
                token=token,
            )
        )
        if isinstance(result, CommitMessageOutput):
            deps.git_svc.commit(
                mount_path,
                deps.repo_root,
                _planned_commit_subject(step, issue, result.message),
            )
            RoleSession(
                mount_path, step.role
            ).clear_provider_state_and_signal_completion()


@dataclasses.dataclass
class _BoundedAgentRunner:
    semaphore: asyncio.Semaphore | None
    agent_runner: AgentRunnerProtocol
    on_started: Callable[[str], None] | None
    _implement_started: bool = dataclasses.field(default=False, init=False)
    _review_started: bool = dataclasses.field(default=False, init=False)

    async def run(self, request: RunRequest) -> AgentSuccessOutput:
        async with self.semaphore or contextlib.nullcontext():
            if self.on_started is not None:
                if (
                    request.role == AgentRole.IMPLEMENTER
                    and not self._implement_started
                ):
                    self.on_started("implement")
                    self._implement_started = True
                elif request.role == AgentRole.REVIEWER and not self._review_started:
                    self.on_started("review")
                    self._review_started = True
            return await self.agent_runner.run(request)


@dataclasses.dataclass
class _RunIssueContext:
    semaphore: asyncio.Semaphore | None = None
    worktree_semaphore: asyncio.Semaphore | None = None
    token: CancellationToken | None = None
    branch_locks: dict[str, asyncio.Lock] | None = None
    on_started: Callable[[str], None] | None = None


async def run_issue(
    issue: dict,
    deps: _ImplementDeps,
    sha: str | None,
    ctx: _RunIssueContext | None = None,
) -> dict:
    _ctx = ctx or _RunIssueContext()
    worktree_semaphore = _ctx.worktree_semaphore
    _branch = branch_for(issue["number"])
    _token = _ctx.token if _ctx.token is not None else CancellationToken()
    _resolve_slice(issue, deps.cfg)

    _runner = _BoundedAgentRunner(
        semaphore=_ctx.semaphore,
        agent_runner=deps.agent_runner,
        on_started=_ctx.on_started,
    )

    lock = await _acquire_branch_lock(_ctx.branch_locks, _branch)
    try:
        _wt_path = worktree_identity(_branch, deps.repo_root).path

        issue_plan = plan_issue_execution_from_worktree(
            issue=issue,
            deps=deps,
            sha=sha,
            worktree_path=_wt_path,
        )

        if issue_plan.issue_outcome == "complete":
            return issue

        for step in issue_plan.run_steps:
            await _execute_role_step(
                issue=issue,
                step=step,
                runner=_runner,
                deps=deps,
                token=_token,
                worktree_semaphore=worktree_semaphore,
            )
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
                _RunIssueContext(
                    semaphore=semaphore,
                    worktree_semaphore=worktree_semaphore,
                    token=_token,
                    branch_locks=branch_locks,
                    on_started=_on_started,
                ),
            )
            for issue in issues
        ],
        return_exceptions=True,
    )
    from pycastle.iteration._implement_dispatch_classify import (  # noqa: PLC0415 — local import breaks circular dependency
        ClassifyFatal,
        classify,
    )

    outcome = classify(issues, list(results), log_error=deps.logger.log_error)
    if isinstance(outcome, ClassifyFatal):
        raise outcome.exception
    return outcome.result
