import asyncio
import contextlib
import dataclasses
import shutil
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from ..agent_output_protocol import AgentRole, CommitMessageOutput
from ..agent_result import CancellationToken, PreflightFailure
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import BranchCollisionError, UsageLimitError
from ..prompt_pipeline import load_standards
from ..session_resume import has_resumable_session
from ..status_display import StatusDisplay
from ..services import GitService
from ..worktree import (
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
    repo_root: Path
    logger: Logger


def branch_for(issue_number: int) -> str:
    return f"pycastle/issue-{issue_number}"


def _format_feedback_commands(checks: Sequence[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


def format_issue_comments(comments: Sequence[dict[str, str]]) -> str:
    parts: list[str] = []
    for c in comments:
        author = c.get("author") or "unknown"
        when = c.get("created_at") or "unknown time"
        body = c.get("body") or ""
        parts.append(f"## Comment by @{author} at {when}\n\n{body}")
    return "\n\n".join(parts)


def _clear_session_dir(session_dir: Path) -> None:
    """Clear contents of role session dir, leaving the dir as the stage-done signal."""
    if not session_dir.is_dir():
        return
    for child in list(session_dir.iterdir()):
        try:
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
        except Exception:
            pass


@dataclasses.dataclass
class ImplementResult:
    completed: list[dict]
    errors: list[tuple[dict, Exception | PreflightFailure]]
    usage_limit_hit: bool = False
    usage_limit_reset_time: datetime | None = None


async def run_issue(
    issue: dict,
    deps: _ImplementDeps,
    semaphore: asyncio.Semaphore | None = None,
    *,
    token: CancellationToken | None = None,
    sha: str | None = None,
    branch_locks: dict[str, asyncio.Lock] | None = None,
    on_started: Callable[[], None] | None = None,
) -> dict | PreflightFailure:
    _branch = branch_for(issue["number"])
    _token = token if token is not None else CancellationToken()
    _standards = load_standards(deps.cfg.prompts_dir)
    prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "ISSUE_BODY": str(issue.get("body") or ""),
        "ISSUE_COMMENTS": format_issue_comments(issue.get("comments") or []),
        "BRANCH": _branch,
        "FEEDBACK_COMMANDS": _format_feedback_commands(deps.cfg.implement_checks),
        **_standards,
    }

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

        _impl_session_dir = _wt_path / ".pycastle-session" / "implementer"
        _review_session_dir = _wt_path / ".pycastle-session" / "reviewer"

        implement_done = _impl_session_dir.is_dir() and not has_resumable_session(
            _impl_session_dir
        )
        review_done = _review_session_dir.is_dir() and not has_resumable_session(
            _review_session_dir
        )

        if review_done:
            return issue

        if not implement_done:
            async with managed_worktree(
                _wt_name,
                branch=_branch,
                sha=sha,
                delete_branch_on_teardown=False,
                deps=deps,
            ) as impl_mount_path:
                _impl_overlay = patch_gitdir_for_container(impl_mount_path)
                try:
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
                    if isinstance(result, CommitMessageOutput):
                        _msg = result.message or issue["title"]
                        deps.git_svc.commit(
                            impl_mount_path,
                            deps.repo_root,
                            f"Implement #{issue['number']} - {_msg}",
                        )
                        _clear_session_dir(
                            impl_mount_path / ".pycastle-session" / "implementer"
                        )
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
            try:
                review_prompt_args = {
                    **prompt_args,
                    "DIFF": deps.git_svc.get_diff_to_main(review_mount_path),
                }
                review_result = await _bounded_run_agent(
                    RunRequest(
                        name=f"Review Agent #{issue['number']}",
                        prompt_file=deps.cfg.prompts_dir / "review-prompt.md",
                        mount_path=review_mount_path,
                        role=AgentRole.REVIEWER,
                        prompt_args=review_prompt_args,
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
                if isinstance(review_result, CommitMessageOutput):
                    _rev_msg = review_result.message or issue["title"]
                    deps.git_svc.commit(
                        review_mount_path,
                        deps.repo_root,
                        f"Review #{issue['number']} - {_rev_msg}",
                    )
                    _clear_session_dir(
                        review_mount_path / ".pycastle-session" / "reviewer"
                    )
            finally:
                if _review_overlay is not None:
                    _review_overlay.unlink(missing_ok=True)
    finally:
        if lock is not None and lock.locked():
            lock.release()

    return issue


async def implement_phase(
    issues: list[dict],
    sha: str | None,
    deps: _ImplementDeps,
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
                semaphore,
                token=_token,
                sha=sha,
                branch_locks=branch_locks,
                on_started=_on_started,
            )
            for issue in issues
        ],
        return_exceptions=True,
    )
    usage_limit_errors = [r for r in results if isinstance(r, UsageLimitError)]
    usage_limit_hit = bool(usage_limit_errors)
    usage_limit_reset_time = next(
        (e.reset_time for e in usage_limit_errors if e.reset_time is not None),
        None,
    )
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
        completed=completed,
        errors=errors,
        usage_limit_hit=usage_limit_hit,
        usage_limit_reset_time=usage_limit_reset_time,
    )
