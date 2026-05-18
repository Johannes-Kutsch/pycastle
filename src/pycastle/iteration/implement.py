import asyncio
import contextlib
import dataclasses
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..agents.output_protocol import AgentRole, CommitMessageOutput
from ..agents.result import CancellationToken
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import (
    AgentFailedError,
    BranchCollisionError,
    InvalidSliceLabelError,
    UsageLimitError,
)
from ..prompts.pipeline import PromptTemplate, build_issue_scope_args, build_wip_clause
from ..agents.classifier import WellFormed, classify_slice
from ..session import RoleSession, is_stage_done_for
from ..display.status_display import StatusDisplay
from ..services import GitService, GithubService
from ..infrastructure.worktree import (
    managed_worktree,
    patch_gitdir_for_container,
    worktree_name_for_branch,
    worktree_path,
)
from ._deps import Logger


class _ImplementDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    git_svc: GitService
    github_svc: GithubService
    repo_root: Path
    logger: Logger


def branch_for(issue_number: int) -> str:
    return f"pycastle/issue-{issue_number}"


def _resolve_slice(issue: dict, cfg: Config) -> tuple[str, PromptTemplate]:
    result = classify_slice(issue, cfg)
    if isinstance(result, WellFormed):
        return result.mode.display_name, result.mode.template
    slice_map_keys = [
        cfg.refactor_slice_label,
        cfg.behavior_slice_label,
        cfg.docs_slice_label,
    ]
    issue_labels: list[str] = issue.get("labels", [])
    matches = result.found
    if not matches:
        detail = f"expected one of {slice_map_keys}, got labels={issue_labels}"
    else:
        detail = f"multiple slice-mode labels found: {matches}"
    raise InvalidSliceLabelError(
        f"Issue #{issue['number']}: invalid slice-mode label — {detail}"
    )


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


async def run_issue(
    issue: dict,
    deps: _ImplementDeps,
    sha: str,
    semaphore: asyncio.Semaphore | None = None,
    *,
    token: CancellationToken | None = None,
    branch_locks: dict[str, asyncio.Lock] | None = None,
    on_started: Callable[[], None] | None = None,
) -> dict:
    _branch = branch_for(issue["number"])
    _token = token if token is not None else CancellationToken()

    def _scope_args_for(mount_path: Path, role: AgentRole) -> dict[str, str]:
        wip = build_wip_clause(
            deps.git_svc.get_branch_commit_subjects(_branch, deps.repo_root),
            RoleSession(mount_path, role).is_resumable(),
            role=role.value,
            issue_number=issue["number"],
        )
        return build_issue_scope_args(
            issue,
            extra_scope_args={"BRANCH": _branch, "WIP_COMMITS": wip},
        )

    _started_fired = False

    async def _bounded_run_agent(request: RunRequest) -> Any:
        nonlocal _started_fired
        async with semaphore or contextlib.nullcontext():
            if not _started_fired and on_started is not None:
                on_started()
                _started_fired = True
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
        _wt_name = worktree_name_for_branch(_branch)
        _wt_path = worktree_path(_wt_name, deps)

        implement_done = is_stage_done_for(_wt_path, AgentRole.IMPLEMENTER)
        review_done = is_stage_done_for(_wt_path, AgentRole.REVIEWER)

        if review_done:
            return issue

        _slice_mode, _impl_template = _resolve_slice(issue, deps.cfg)

        if not implement_done:
            async with managed_worktree(
                _wt_name,
                branch=_branch,
                sha=sha,
                delete_branch_on_teardown=False,
                deps=deps,
            ) as impl_mount_path:
                _impl_overlay = patch_gitdir_for_container(impl_mount_path)
                _impl_scope_args = _scope_args_for(
                    impl_mount_path, AgentRole.IMPLEMENTER
                )
                try:
                    result = await _bounded_run_agent(
                        RunRequest(
                            name=f"Implement Agent #{issue['number']}",
                            template=_impl_template,
                            mount_path=impl_mount_path,
                            role=AgentRole.IMPLEMENTER,
                            scope_args=_impl_scope_args,
                            model=deps.cfg.implement_override.model,
                            effort=deps.cfg.implement_override.effort,
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
                finally:
                    if _impl_overlay is not None:
                        _impl_overlay.unlink(missing_ok=True)

        async with managed_worktree(
            _wt_name,
            branch=_branch,
            sha=None,
            delete_branch_on_teardown=False,
            deps=deps,
        ) as review_mount_path:
            _review_overlay = patch_gitdir_for_container(review_mount_path)
            _review_scope_args = _scope_args_for(review_mount_path, AgentRole.REVIEWER)
            try:
                review_result = await _bounded_run_agent(
                    RunRequest(
                        name=f"Review Agent #{issue['number']}",
                        template=PromptTemplate.REVIEW,
                        mount_path=review_mount_path,
                        role=AgentRole.REVIEWER,
                        scope_args=_review_scope_args,
                        model=deps.cfg.review_override.model,
                        effort=deps.cfg.review_override.effort,
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
                if _review_overlay is not None:
                    _review_overlay.unlink(missing_ok=True)
    finally:
        if lock is not None and lock.locked():
            lock.release()

    return issue


async def implement_phase(
    issues: list[dict],
    deps: _ImplementDeps,
    sha: str,
    *,
    token: CancellationToken | None = None,
) -> ImplementResult:
    _token = token if token is not None else CancellationToken()
    semaphore = asyncio.Semaphore(deps.cfg.max_parallel)
    branch_locks: dict[str, asyncio.Lock] = {}
    total = len(issues)
    started = 0
    deps.status_display.update_phase(
        "Implement", f"Running: started Agents for 0/{total} issues"
    )

    def _on_started() -> None:
        nonlocal started
        started += 1
        deps.status_display.update_phase(
            "Implement", f"Running: started Agents for {started}/{total} issues"
        )

    results = await asyncio.gather(
        *[
            run_issue(
                issue,
                deps,
                sha,
                semaphore,
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
    usage_limit_errors = [r for r in results if isinstance(r, UsageLimitError)]
    usage_limit_hit = bool(usage_limit_errors)
    usage_limit_reset_time = next(
        (e.reset_time for e in usage_limit_errors if e.reset_time is not None),
        None,
    )
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
    )
