import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..agents.runner import AgentRunnerProtocol
from ..config import Config
from ..services import GitCommandError, GitService, GithubService
from ..display.status_display import StatusDisplay
from ..infrastructure.worktree import teardown_worktree, worktree_identity
from ._merge_conflict_recovery import (
    recover_conflicts,
)
from ._merge_reporting import MergeProgressReporter, build_merge_close_message
from ._rows import status_row
from ._utils import _wait_for_clean_working_tree
from .implement import branch_for
from .preflight import PreflightAFK, PreflightCache, PreflightHITL


class _MergeDeps(Protocol):
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    preflight_cache: PreflightCache


@dataclasses.dataclass
class MergeResult:
    clean: list[dict]
    conflicts: list[dict]
    completed_conflicts: list[dict] = dataclasses.field(default_factory=list)
    pending_conflicts: list[dict] = dataclasses.field(default_factory=list)
    preflight_blocker: PreflightHITL | PreflightAFK | None = None


def _classify_merge_candidates(
    completed: list[dict], deps: _MergeDeps
) -> tuple[list[dict], list[dict]]:
    clean_issues: list[dict] = []
    conflict_issues: list[dict] = []
    for issue in completed:
        if deps.git_svc.try_merge(deps.repo_root, branch_for(issue["number"])):
            clean_issues.append(issue)
        else:
            conflict_issues.append(issue)
    return clean_issues, conflict_issues


def _build_merge_result(
    *,
    clean_issues: list[dict],
    conflict_issues: list[dict],
    recovery_result: dict[str, list[dict]] | None = None,
    preflight_blocker: PreflightHITL | PreflightAFK | None = None,
) -> MergeResult:
    return MergeResult(
        clean=clean_issues,
        conflicts=conflict_issues,
        preflight_blocker=preflight_blocker,
        **(recovery_result or {}),
    )


def _should_auto_push(
    *,
    auto_push: bool,
    clean_issues: list[dict],
    conflict_issues: list[dict],
    pending_conflicts: list[dict],
) -> bool:
    return (
        auto_push
        and bool(clean_issues or conflict_issues)
        and not (conflict_issues and pending_conflicts)
    )


async def _delete_merged_branches(
    branches: list[str],
    deps: _MergeDeps,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    total = len(branches)
    done = 0
    slots: list[str | None] = [None] * total
    registered_worktrees = deps.git_svc.list_worktrees(deps.repo_root)

    async def _teardown_one(branch: str, idx: int) -> None:
        nonlocal done
        try:
            if not deps.git_svc.is_ancestor(branch, deps.repo_root):
                return
            worktree_path_ = worktree_identity(branch, deps.repo_root).path
            if worktree_path_ in registered_worktrees:
                try:
                    await asyncio.to_thread(
                        teardown_worktree, deps.git_svc, deps.repo_root, worktree_path_
                    )
                except Exception as e:
                    deps.status_display.print(
                        "Merge",
                        f"Warning: could not remove worktree for {branch!r}: {e}",
                        "warning",
                    )

            try:
                await asyncio.to_thread(
                    deps.git_svc.delete_branch, branch, deps.repo_root
                )
                slots[idx] = branch
            except GitCommandError as e:
                deps.status_display.print(
                    "Merge",
                    f"Warning: could not delete branch {branch!r}: {e}",
                    "warning",
                )
        finally:
            done += 1
            if on_progress is not None:
                on_progress(done, total)

    results = await asyncio.gather(
        *[_teardown_one(b, i) for i, b in enumerate(branches)],
        return_exceptions=True,
    )
    for branch, r in zip(branches, results, strict=True):
        if isinstance(r, BaseException):
            deps.status_display.print(
                "Merge",
                f"Warning: teardown of {branch!r} failed: {r}",
                "warning",
            )
    return [s for s in slots if s is not None]


async def _close_issues_parallel(
    issues: list[dict],
    github_svc: GithubService,
    on_progress: Callable[[int, int], None] | None = None,
    on_error: Callable[[int, BaseException], None] | None = None,
) -> None:
    n = len(issues)
    done = 0

    async def _close_one(issue: dict) -> None:
        nonlocal done
        await asyncio.to_thread(github_svc.close_issue, issue["number"])
        done += 1
        if on_progress is not None:
            on_progress(done, n)

    results = await asyncio.gather(
        *[_close_one(i) for i in issues], return_exceptions=True
    )
    for issue, r in zip(issues, results, strict=True):
        if isinstance(r, BaseException):
            if on_error is not None:
                on_error(issue["number"], r)


async def merge_phase(completed: list[dict], deps: _MergeDeps) -> MergeResult:
    async with status_row(
        deps.status_display,
        "Merge",
        kind="phase",
        must_close=True,
        initial_phase="Merging",
    ) as row:
        await _wait_for_clean_working_tree(deps, "Merge")
        completed_total = len(completed)
        clean_issues, conflict_issues = _classify_merge_candidates(completed, deps)

        progress = MergeProgressReporter(
            status_display=deps.status_display,
            completed_total=completed_total,
            merge_done=len(clean_issues),
        )
        progress.render()

        def _on_close_error(issue_number: int, exc: BaseException) -> None:
            deps.status_display.print(
                "Merge",
                f"Warning: could not close issue #{issue_number}: {exc}",
                "warning",
            )

        def _close_merge_row(summary: str) -> None:
            row.close("finished")
            deps.status_display.print("Merge", summary, "success")

        async def _close_issues(issues: list[dict]) -> None:
            batch_start = progress.close_done

            def _on_progress(done: int, total: int) -> None:
                progress.update_close_done(batch_start + done)

            await _close_issues_parallel(
                issues, deps.github_svc, _on_progress, _on_close_error
            )

        async def _delete_branches(branches: list[str]) -> list[str]:
            batch_start = progress.remove_done or 0

            def _on_teardown_progress(done: int, total: int) -> None:
                progress.update_remove_done(batch_start + done)

            deleted = await _delete_merged_branches(
                branches, deps, _on_teardown_progress
            )
            progress.update_remove_done(None)
            return deleted

        if clean_issues:
            await _close_issues(clean_issues)
            deps.github_svc.close_completed_parent_issues()

        clean_deleted = await _delete_branches(
            [branch_for(i["number"]) for i in clean_issues]
        )

        if not conflict_issues:
            _close_merge_row(build_merge_close_message(clean_deleted))
        else:
            verdict = await deps.preflight_cache.get_safe_sha(deps)
            if isinstance(verdict, (PreflightHITL, PreflightAFK)):
                deps.status_display.print(
                    "Merge",
                    "Merge-time preflight failed; skipping conflict branch merge. "
                    "Conflict issues remain open for recovery in the next iteration.",
                )
                _close_merge_row(build_merge_close_message(clean_deleted))
                if deps.cfg.auto_push and clean_issues:
                    await deps.git_svc.push(
                        deps.repo_root,
                        resolver=lambda: deps.preflight_cache.pull_with_resolution(
                            deps
                        ),
                    )
                return _build_merge_result(
                    clean_issues=clean_issues,
                    conflict_issues=conflict_issues,
                    recovery_result={"pending_conflicts": conflict_issues},
                    preflight_blocker=verdict,
                )

            recovery = await recover_conflicts(
                conflict_issues=conflict_issues,
                progress=progress,
                deps=deps,
            )
            if recovery.has_completed_conflicts:
                deps.github_svc.close_completed_parent_issues()
            _close_merge_row(
                build_merge_close_message(
                    clean_deleted + recovery.deleted_conflict_branches,
                    **recovery.close_message_kwargs(),
                )
            )

        recovery_result = (
            recovery.merge_result_kwargs()
            if conflict_issues
            else {"pending_conflicts": []}
        )
        pending_conflicts = recovery_result["pending_conflicts"]
        if _should_auto_push(
            auto_push=deps.cfg.auto_push,
            clean_issues=clean_issues,
            conflict_issues=conflict_issues,
            pending_conflicts=pending_conflicts,
        ):
            await deps.git_svc.push(
                deps.repo_root,
                resolver=lambda: deps.preflight_cache.pull_with_resolution(deps),
            )
        return _build_merge_result(
            clean_issues=clean_issues,
            conflict_issues=conflict_issues,
            recovery_result=recovery_result,
        )
