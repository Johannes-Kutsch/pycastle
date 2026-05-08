import asyncio
import dataclasses
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..agent_output_protocol import AgentRole
from ..agent_result import PreflightFailure
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..services import GitCommandError, GitService, GithubService
from ..session_resume import clear_session_dir
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


class _MergeDeps(Protocol):
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path


MERGE_SANDBOX = "pycastle/merge-sandbox"


@dataclasses.dataclass
class MergeResult:
    clean: list[dict]
    conflicts: list[dict]


def _delete_merged_branches(branches: list[str], deps: _MergeDeps) -> list[str]:
    deleted: list[str] = []
    registered_worktrees = deps.git_svc.list_worktrees(deps.repo_root)
    for branch in branches:
        if not deps.git_svc.is_ancestor(branch, deps.repo_root):
            continue
        worktree_path_ = worktree_path(worktree_name_for_branch(branch), deps)
        if worktree_path_ in registered_worktrees:
            try:
                teardown_worktree(deps.git_svc, deps.repo_root, worktree_path_)
            except Exception as e:
                print(
                    f"Warning: could not remove worktree for {branch!r}: {e}",
                    file=sys.stderr,
                )

        try:
            deps.git_svc.delete_branch(branch, deps.repo_root)
            deleted.append(branch)
        except GitCommandError as e:
            print(f"Warning: could not delete branch {branch!r}: {e}", file=sys.stderr)
    return deleted


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
    for r in results:
        if isinstance(r, BaseException):
            raise r


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

        def _on_progress(done: int, total: int) -> None:
            deps.status_display.update_phase("Merge", f"Closing {done}/{total} issues")

        if clean_issues:
            await _close_issues_parallel(clean_issues, deps.github_svc, _on_progress)
            deps.github_svc.close_completed_parent_issues()

        clean_deleted = _delete_merged_branches(
            [branch_for(i["number"]) for i in clean_issues], deps
        )

        if not conflict_issues:
            row.close(_build_close_message(clean_deleted))
        else:
            target_branch = deps.git_svc.get_current_branch(deps.repo_root)
            sha = deps.git_svc.get_head_sha(deps.repo_root)
            async with managed_worktree(
                "merge-sandbox",
                branch=MERGE_SANDBOX,
                sha=sha,
                delete_branch_on_teardown=True,
                deps=deps,
            ) as sandbox_path:
                merger_result = await deps.agent_runner.run(
                    RunRequest(
                        name="Merge Agent",
                        prompt_file=deps.cfg.prompts_dir / "merge-prompt.md",
                        mount_path=sandbox_path,
                        role=AgentRole.MERGER,
                        prompt_args={
                            "BRANCHES": "\n".join(
                                f"- {branch_for(i['number'])}" for i in conflict_issues
                            ),
                            "CHECKS": " && ".join(
                                cmd for _, cmd in deps.cfg.preflight_checks
                            ),
                        },
                        model=deps.cfg.merge_override.model,
                        status_display=deps.status_display,
                        effort=deps.cfg.merge_override.effort,
                        stage="pre-merge",
                        work_body=f"Merging {len(conflict_issues)} Branches",
                    )
                )
                if isinstance(merger_result, PreflightFailure):
                    deps.status_display.print(
                        "Merge",
                        "Merge-time preflight failed; skipping conflict branch merge. "
                        "Conflict issues remain open for recovery in the next iteration.",
                    )
                    row.close(_build_close_message(clean_deleted))
                    if deps.cfg.auto_push and clean_issues:
                        deps.git_svc.push(deps.repo_root)
                    return MergeResult(clean=clean_issues, conflicts=conflict_issues)
                deps.git_svc.fast_forward_branch(
                    deps.repo_root, target_branch, MERGE_SANDBOX
                )
                clear_session_dir(sandbox_path / ".pycastle-session" / "merger")
            conflict_deleted = _delete_merged_branches(
                [branch_for(i["number"]) for i in conflict_issues], deps
            )
            await _close_issues_parallel(conflict_issues, deps.github_svc, _on_progress)
            deps.github_svc.close_completed_parent_issues()
            row.close(_build_close_message(clean_deleted + conflict_deleted))

        if deps.cfg.auto_push and (clean_issues or conflict_issues):
            deps.git_svc.push(deps.repo_root)
        return MergeResult(clean=clean_issues, conflicts=conflict_issues)
