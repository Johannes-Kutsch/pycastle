import dataclasses
import sys

from ..agent_output_protocol import AgentRole
from ..agent_result import PreflightFailure
from ..agent_runner import RunRequest
from ..services import GitCommandError
from ..worktree import (
    branch_worktree,
    remove_worktrees_dir_if_empty,
    worktree_name_for_branch,
    worktree_path,
)
from ._deps import Deps
from ._phase_row import phase_row
from ._utils import _wait_for_clean_working_tree
from .implement import branch_for

MERGE_SANDBOX = "pycastle/merge-sandbox"


@dataclasses.dataclass
class MergeResult:
    clean: list[dict]
    conflicts: list[dict]


def _delete_merged_branches(branches: list[str], deps: Deps) -> list[str]:
    deleted: list[str] = []
    registered_worktrees = deps.git_svc.list_worktrees(deps.repo_root)
    for branch in branches:
        if not deps.git_svc.is_ancestor(branch, deps.repo_root):
            continue
        worktree_path_ = worktree_path(worktree_name_for_branch(branch), deps)
        if worktree_path_ in registered_worktrees:
            try:
                deps.git_svc.remove_worktree(deps.repo_root, worktree_path_)
                remove_worktrees_dir_if_empty(worktree_path_.parent)
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
        return "Execution complete, 0 branch(es) deleted"
    header = f"Execution complete, {len(deleted)} branch(es) deleted:"
    lines = "\n".join(f"  Deleted merged branch: {b}" for b in deleted)
    return f"{header}\n{lines}"


async def merge_phase(completed: list[dict], deps: Deps) -> MergeResult:
    async with phase_row(deps.status_display, "Merge", initial_phase="Merging") as row:
        await _wait_for_clean_working_tree(deps, "Merge")

        conflict_issues: list[dict] = []
        for issue in completed:
            if deps.git_svc.try_merge(deps.repo_root, branch_for(issue["number"])):
                deps.github_svc.close_issue(issue["number"])
            else:
                conflict_issues.append(issue)

        clean_issues = [i for i in completed if i not in conflict_issues]

        if clean_issues:
            deps.github_svc.close_completed_parent_issues()

        clean_deleted = _delete_merged_branches(
            [branch_for(i["number"]) for i in clean_issues], deps
        )

        if not conflict_issues:
            row.close(_build_close_message(clean_deleted))
        else:
            target_branch = deps.git_svc.get_current_branch(deps.repo_root)
            sha = deps.git_svc.get_head_sha(deps.repo_root)
            async with branch_worktree(
                "merge-sandbox", MERGE_SANDBOX, sha, deps
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
            conflict_deleted = _delete_merged_branches(
                [branch_for(i["number"]) for i in conflict_issues], deps
            )
            for issue in conflict_issues:
                deps.github_svc.close_issue(issue["number"])
            deps.github_svc.close_completed_parent_issues()
            row.close(_build_close_message(clean_deleted + conflict_deleted))

        if deps.cfg.auto_push and (clean_issues or conflict_issues):
            deps.git_svc.push(deps.repo_root)
        return MergeResult(clean=clean_issues, conflicts=conflict_issues)
