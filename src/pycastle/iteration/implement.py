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
from ..managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import (
    build_per_issue_scope_args,
)
from ..issue_readiness import ready_slice_outcome_for_issue
from ..session import RoleSession, is_stage_done_for
from ..session import RunKind
from ..session.service_session_store import (
    has_exact_provider_transcript_for_selected_service,
)
from ..display.status_display import StatusDisplay
from ..services import GitService, GithubService
from ..infrastructure.worktree import (
    DurableIssueWorktreeIntent,
    durable_issue_worktree,
    issue_branch,
    worktree_identity,
)
from ._deps import Logger

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
    ready = ready_slice_outcome_for_issue(issue, cfg)
    if ready is None:
        raise RuntimeError(
            f"Issue #{issue['number']} is not implement-ready: missing a ready "
            "slice-mode selection."
        )
    return (ready.display_name, ready.template)


def pick_implement_template(issue: dict, cfg: Config) -> PromptTemplate:
    return _resolve_slice(issue, cfg)[1]


def pick_slice_mode(issue: dict, cfg: Config) -> str:
    return _resolve_slice(issue, cfg)[0]


def _resolved_stage_service_name(cfg: Config, role: AgentRole) -> str:
    if role is AgentRole.IMPLEMENTER:
        return cfg.implement_override.service
    if role is AgentRole.REVIEWER:
        return cfg.review_override.service
    raise RuntimeError(f"Unsupported role {role!r} for implement path")


def _prompt_run_state_for_role(
    *,
    mount_path: Path,
    role: AgentRole,
    deps: _ImplementDeps,
) -> tuple[RunKind, bool]:
    role_session = RoleSession(mount_path, role)
    service_name = _resolved_stage_service_name(deps.cfg, role)
    has_resumable_state = role_session.is_resumable()
    has_exact_transcript_handoff = has_exact_provider_transcript_for_selected_service(
        worktree=mount_path,
        role=role,
        namespace="",
        registry=deps.service_registry,
        service_name=service_name,
    )
    run_kind = (
        role_session.run_kind()
        if has_exact_transcript_handoff or not has_resumable_state
        else RunKind.FRESH
    )
    interrupted_work_from_dirty_tree = (
        run_kind is RunKind.FRESH
        and has_resumable_state
        and not has_exact_transcript_handoff
        and not deps.git_svc.is_working_tree_clean(mount_path)
    )
    return run_kind, interrupted_work_from_dirty_tree


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

    def _scope_args_for(mount_path: Path, role: AgentRole) -> dict[str, str]:
        run_kind, interrupted_work_from_dirty_tree = _prompt_run_state_for_role(
            mount_path=mount_path,
            role=role,
            deps=deps,
        )
        return build_per_issue_scope_args(
            issue,
            branch=_branch,
            run_kind=run_kind,
            is_dirty=interrupted_work_from_dirty_tree,
        )

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

        implement_done = is_stage_done_for(_wt_path, AgentRole.IMPLEMENTER)
        review_done = is_stage_done_for(_wt_path, AgentRole.REVIEWER)

        if review_done:
            return issue

        _slice_mode, _impl_template = _resolve_slice(issue, deps.cfg)

        if not implement_done:
            async with (
                worktree_semaphore or contextlib.nullcontext(),
                durable_issue_worktree(
                    issue["number"],
                    intent=DurableIssueWorktreeIntent.IMPLEMENTER,
                    deps=deps,
                    planner_sha=sha,
                ) as impl_mount_path,
            ):
                _impl_scope_args = _scope_args_for(
                    impl_mount_path, AgentRole.IMPLEMENTER
                )
                mount_decision = decide_managed_worktree_mount(
                    repo_root=deps.repo_root,
                    mount_path=impl_mount_path,
                    caller=f"Implement Agent #{issue['number']}",
                    role=AgentRole.IMPLEMENTER.value,
                )
                if isinstance(
                    mount_decision, ManagedWorktreeMountRejected
                ) and should_reject_managed_worktree_mount(mount_decision):
                    raise SetupPhaseError(
                        AgentRole.IMPLEMENTER.value,
                        describe_managed_worktree_mount_rejection(mount_decision),
                    )
                result = await _bounded_run_agent(
                    RunRequest(
                        name=f"Implement Agent #{issue['number']}",
                        prompt=build_prompt_invocation(
                            _impl_template, _impl_scope_args
                        ),
                        mount_path=impl_mount_path,
                        role=AgentRole.IMPLEMENTER,
                        model=deps.cfg.implement_override.model,
                        effort=deps.cfg.implement_override.effort,
                        service=deps.cfg.implement_override.service,
                        stage="pre-implementation",
                        status_display=deps.status_display,
                        issue_title=issue["title"],
                        work_body=f'implementing {_slice_mode} "{issue["title"]}"',
                        token=_token,
                    )
                )
                if isinstance(result, CommitMessageOutput):
                    _msg = result.message or issue["title"]
                    deps.git_svc.commit(
                        impl_mount_path,
                        deps.repo_root,
                        f"Implement #{issue['number']} - {_msg}",
                    )
                    RoleSession(impl_mount_path, AgentRole.IMPLEMENTER).mark_done()

        async with (
            worktree_semaphore or contextlib.nullcontext(),
            durable_issue_worktree(
                issue["number"],
                intent=DurableIssueWorktreeIntent.REVIEWER,
                deps=deps,
            ) as review_mount_path,
        ):
            _review_scope_args = _scope_args_for(review_mount_path, AgentRole.REVIEWER)
            mount_decision = decide_managed_worktree_mount(
                repo_root=deps.repo_root,
                mount_path=review_mount_path,
                caller=f"Review Agent #{issue['number']}",
                role=AgentRole.REVIEWER.value,
            )
            if isinstance(
                mount_decision, ManagedWorktreeMountRejected
            ) and should_reject_managed_worktree_mount(mount_decision):
                raise SetupPhaseError(
                    AgentRole.REVIEWER.value,
                    describe_managed_worktree_mount_rejection(mount_decision),
                )
            review_result = await _bounded_run_agent(
                RunRequest(
                    name=f"Review Agent #{issue['number']}",
                    prompt=build_prompt_invocation(
                        PromptTemplate.REVIEW,
                        _review_scope_args,
                    ),
                    mount_path=review_mount_path,
                    role=AgentRole.REVIEWER,
                    model=deps.cfg.review_override.model,
                    effort=deps.cfg.review_override.effort,
                    service=deps.cfg.review_override.service,
                    stage="pre-review",
                    status_display=deps.status_display,
                    issue_title=issue["title"],
                    work_body=f'reviewing {_slice_mode} "{issue["title"]}"',
                    token=_token,
                )
            )
            if isinstance(review_result, CommitMessageOutput):
                _rev_msg = review_result.message or issue["title"]
                deps.git_svc.commit(
                    review_mount_path,
                    deps.repo_root,
                    f"Review #{issue['number']} - {_rev_msg}",
                )
                RoleSession(review_mount_path, AgentRole.REVIEWER).mark_done()
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
