import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..agent_output_protocol import AgentRole
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompt_pipeline import PromptTemplate
from ..services import GitCommandError, GitService, GithubService
from ..session_resume import RoleSession
from ..status_display import StatusDisplay
from ..worktree import (
    managed_worktree,
    teardown_worktree,
    worktree_name_for_branch,
    worktree_path,
)
from ._rows import phase_row
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


MERGE_SANDBOX = "pycastle/merge-sandbox"


@dataclasses.dataclass
class MergeResult:
    clean: list[dict]
    conflicts: list[dict]


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
            worktree_path_ = worktree_path(worktree_name_for_branch(branch), deps)
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


def _build_close_message(deleted: list[str]) -> str:
    if not deleted:
        return "Execution complete, 0 branch(es) merged and deleted"
    header = f"Execution complete, {len(deleted)} branch(es) merged and deleted:"
    lines = "\n".join(f"  Deleted merged branch: {b}" for b in deleted)
    return f"{header}\n{lines}"


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
    async with phase_row(deps.status_display, "Merge", initial_phase="Merging") as row:
        await _wait_for_clean_working_tree(deps, "Merge")

        clean_issues: list[dict] = []
        conflict_issues: list[dict] = []
        for issue in completed:
            if deps.git_svc.try_merge(deps.repo_root, branch_for(issue["number"])):
                clean_issues.append(issue)
            else:
                conflict_issues.append(issue)

        close_done = 0
        close_total = 0

        def _on_progress(done: int, total: int) -> None:
            nonlocal close_done, close_total
            close_done = done
            close_total = total
            deps.status_display.update_phase("Merge", f"Closing {done}/{total} issues")

        def _on_teardown_progress(done: int, total: int) -> None:
            deps.status_display.update_phase(
                "Merge",
                f"Closing {close_done}/{close_total} issues, removing {done}/{total} worktrees",
            )

        def _on_close_error(issue_number: int, exc: BaseException) -> None:
            deps.status_display.print(
                "Merge",
                f"Warning: could not close issue #{issue_number}: {exc}",
                "warning",
            )

        if clean_issues:
            await _close_issues_parallel(
                clean_issues, deps.github_svc, _on_progress, _on_close_error
            )
            deps.github_svc.close_completed_parent_issues()

        clean_deleted = await _delete_merged_branches(
            [branch_for(i["number"]) for i in clean_issues], deps, _on_teardown_progress
        )

        if not conflict_issues:
            row.close(_build_close_message(clean_deleted))
        else:
            target_branch = deps.git_svc.get_current_branch(deps.repo_root)
            verdict = await deps.preflight_cache.get_safe_sha(deps)
            if isinstance(verdict, (PreflightHITL, PreflightAFK)):
                deps.status_display.print(
                    "Merge",
                    "Merge-time preflight failed; skipping conflict branch merge. "
                    "Conflict issues remain open for recovery in the next iteration.",
                )
                row.close(_build_close_message(clean_deleted))
                if deps.cfg.auto_push and clean_issues:
                    deps.git_svc.push(deps.repo_root)
                return MergeResult(clean=clean_issues, conflicts=conflict_issues)
            async with managed_worktree(
                "merge-sandbox",
                branch=MERGE_SANDBOX,
                sha=verdict.sha,
                delete_branch_on_teardown=True,
                deps=deps,
            ) as sandbox_path:
                await deps.agent_runner.run(
                    RunRequest(
                        name="Merge Agent",
                        template=PromptTemplate.MERGE,
                        mount_path=sandbox_path,
                        role=AgentRole.MERGER,
                        scope_args={
                            "BRANCHES": "\n".join(
                                f"- {branch_for(i['number'])}" for i in conflict_issues
                            ),
                        },
                        model=deps.cfg.merge_override.model,
                        status_display=deps.status_display,
                        effort=deps.cfg.merge_override.effort,
                        stage="pre-merge",
                        work_body=f"Merging {len(conflict_issues)} Branches",
                    )
                )
                deps.git_svc.fast_forward_branch(
                    deps.repo_root, target_branch, MERGE_SANDBOX
                )
                RoleSession(sandbox_path, AgentRole.MERGER).discard()
            conflict_deleted = await _delete_merged_branches(
                [branch_for(i["number"]) for i in conflict_issues],
                deps,
                _on_teardown_progress,
            )
            await _close_issues_parallel(
                conflict_issues, deps.github_svc, _on_progress, _on_close_error
            )
            deps.github_svc.close_completed_parent_issues()
            row.close(_build_close_message(clean_deleted + conflict_deleted))

        if deps.cfg.auto_push and (clean_issues or conflict_issues):
            deps.git_svc.push(deps.repo_root)
        return MergeResult(clean=clean_issues, conflicts=conflict_issues)
