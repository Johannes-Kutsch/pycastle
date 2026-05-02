import asyncio
import contextlib
import dataclasses
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any

from ..agent_output_protocol import AgentRole
from ..agent_result import CancellationToken, PreflightFailure
from ..agent_runner import RunRequest
from ..errors import BranchCollisionError, UsageLimitError
from ..prompt_utils import load_standards
from ..worktree import (
    patch_gitdir_for_container,
    worktree_name_for_branch,
    worktree_path as _worktree_path,
)
from ._deps import Deps


@asynccontextmanager
async def _agent_worktree(
    branch: str,
    sha: str | None,
    token: CancellationToken,
    deps: Deps,
):
    wt_name = worktree_name_for_branch(branch)
    wt_path = _worktree_path(wt_name, deps)
    deps.git_svc.create_worktree(deps.repo_root, wt_path, branch, sha)
    gitdir_overlay = None
    try:
        gitdir_overlay = patch_gitdir_for_container(wt_path)
        yield wt_path
    finally:
        if not token.wants_worktree_preserved:
            try:
                clean = deps.git_svc.is_working_tree_clean(wt_path)
            except Exception:
                clean = False
            if clean:
                deps.git_svc.remove_worktree(deps.repo_root, wt_path)
        if gitdir_overlay is not None:
            gitdir_overlay.unlink(missing_ok=True)


def branch_for(issue_number: int) -> str:
    return f"pycastle/issue-{issue_number}"


def _format_feedback_commands(checks: Sequence[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


@dataclasses.dataclass
class ImplementResult:
    completed: list[dict]
    errors: list[tuple[dict, Exception | PreflightFailure]]
    usage_limit_hit: bool = False


async def run_issue(
    issue: dict,
    deps: Deps,
    semaphore: asyncio.Semaphore | None = None,
    *,
    token: CancellationToken | None = None,
    sha: str | None = None,
    branch_locks: dict[str, asyncio.Lock] | None = None,
) -> dict | PreflightFailure:
    _branch = branch_for(issue["number"])
    _token = token if token is not None else CancellationToken()
    _standards = load_standards(deps.cfg.prompts_dir)
    prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": _branch,
        "FEEDBACK_COMMANDS": _format_feedback_commands(deps.cfg.implement_checks),
        **_standards,
    }

    async def _bounded_run_agent(request: RunRequest) -> Any:
        async with semaphore or contextlib.nullcontext():
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
        async with _agent_worktree(_branch, sha, _token, deps) as impl_mount_path:
            result = await _bounded_run_agent(
                RunRequest(
                    name=f"Implement Agent #{issue['number']}",
                    prompt_file=deps.cfg.prompts_dir / "implement-prompt.md",
                    mount_path=impl_mount_path,
                    role=AgentRole.IMPLEMENTER,
                    prompt_args=prompt_args,
                    model=deps.cfg.implement_override.model,
                    effort=deps.cfg.implement_override.effort,
                    stage="pre-implementation",
                    skip_preflight=True,
                    status_display=deps.status_display,
                    issue_title=issue["title"],
                    work_body=f'implementing "{issue["title"]}"',
                    token=_token,
                )
            )
            if isinstance(result, PreflightFailure):
                return result

        async with _agent_worktree(_branch, None, _token, deps) as review_mount_path:
            await _bounded_run_agent(
                RunRequest(
                    name=f"Review Agent #{issue['number']}",
                    prompt_file=deps.cfg.prompts_dir / "review-prompt.md",
                    mount_path=review_mount_path,
                    role=AgentRole.REVIEWER,
                    prompt_args=prompt_args,
                    model=deps.cfg.review_override.model,
                    effort=deps.cfg.review_override.effort,
                    stage="pre-review",
                    skip_preflight=True,
                    status_display=deps.status_display,
                    issue_title=issue["title"],
                    work_body=f'reviewing "{issue["title"]}"',
                    token=_token,
                )
            )
    finally:
        if lock is not None and lock.locked():
            lock.release()

    return issue


async def implement_phase(
    issues: list[dict],
    sha: str | None,
    deps: Deps,
    *,
    token: CancellationToken | None = None,
) -> ImplementResult:
    _token = token if token is not None else CancellationToken()
    semaphore = asyncio.Semaphore(deps.cfg.max_parallel)
    branch_locks: dict[str, asyncio.Lock] = {}
    results = await asyncio.gather(
        *[
            run_issue(
                issue, deps, semaphore, token=_token, sha=sha, branch_locks=branch_locks
            )
            for issue in issues
        ],
        return_exceptions=True,
    )
    usage_limit_hit = any(isinstance(r, UsageLimitError) for r in results)
    completed: list[dict] = []
    errors: list[tuple[dict, Exception | PreflightFailure]] = []
    for issue, result in zip(issues, results):
        if isinstance(result, UsageLimitError):
            continue
        elif isinstance(result, (Exception, PreflightFailure)):
            deps.logger.log_error(issue, result)
            errors.append((issue, result))
        elif isinstance(result, dict):
            completed.append(issue)
    return ImplementResult(
        completed=completed, errors=errors, usage_limit_hit=usage_limit_hit
    )
