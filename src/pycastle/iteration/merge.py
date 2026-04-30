import asyncio
import dataclasses
import re
import sys
from pathlib import Path

from ..agent_output_protocol import AgentRole, parse
from ..git_service import GitCommandError
from ._deps import Deps
from .implement import branch_for

MERGE_SANDBOX = "pycastle/merge-sandbox"


@dataclasses.dataclass
class MergeResult:
    clean: list[dict]
    conflicts: list[dict]


async def _wait_for_clean_working_tree(deps: Deps) -> None:
    if deps.git_svc.is_working_tree_clean(deps.repo_root):
        return
    deps.status_display.print(
        "Working tree has uncommitted changes. "
        "Please commit or revert all local changes before the merge phase can proceed."
    )
    while not deps.git_svc.is_working_tree_clean(deps.repo_root):
        await asyncio.sleep(10)


def _worktree_path_for_branch(branch: str, deps: Deps) -> Path:
    m = re.match(r"pycastle/issue-(\d+)", branch)
    worktree_name = (
        f"issue-{m.group(1)}"
        if m
        else re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
    )
    return deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / worktree_name


def _delete_merged_branches(branches: list[str], deps: Deps) -> None:
    registered_worktrees = deps.git_svc.list_worktrees(deps.repo_root)
    for branch in branches:
        if not deps.git_svc.is_ancestor(branch, deps.repo_root):
            continue
        worktree_path = _worktree_path_for_branch(branch, deps)
        if worktree_path in registered_worktrees:
            try:
                deps.git_svc.remove_worktree(deps.repo_root, worktree_path)
            except Exception as e:
                print(
                    f"Warning: could not remove worktree for {branch!r}: {e}",
                    file=sys.stderr,
                )
        try:
            deps.git_svc.delete_branch(branch, deps.repo_root)
            deps.status_display.print(f"Deleted merged branch: {branch}")
        except GitCommandError as e:
            print(f"Warning: could not delete branch {branch!r}: {e}", file=sys.stderr)


async def merge_phase(completed: list[dict], deps: Deps) -> MergeResult:
    await _wait_for_clean_working_tree(deps)

    conflict_issues: list[dict] = []
    for issue in completed:
        if deps.git_svc.try_merge(deps.repo_root, branch_for(issue["number"])):
            deps.github_svc.close_issue(issue["number"])
        else:
            conflict_issues.append(issue)

    clean_issues = [i for i in completed if i not in conflict_issues]

    if clean_issues:
        deps.github_svc.close_completed_parent_issues()

    _delete_merged_branches([branch_for(i["number"]) for i in clean_issues], deps)

    if conflict_issues:
        target_branch = deps.git_svc.get_current_branch(deps.repo_root)
        sha = deps.git_svc.get_head_sha(deps.repo_root)
        worktree_path = (
            deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "merge-sandbox"
        )
        deps.git_svc.create_worktree(deps.repo_root, worktree_path, MERGE_SANDBOX, sha)
        try:
            merger_result = await deps.run_agent(
                name="Merger",
                prompt_file=deps.cfg.prompts_dir / "merge-prompt.md",
                mount_path=worktree_path,
                env=deps.env,
                prompt_args={
                    "BRANCHES": "\n".join(
                        f"- {branch_for(i['number'])}" for i in conflict_issues
                    ),
                    "CHECKS": " && ".join(cmd for _, cmd in deps.cfg.preflight_checks),
                },
                model=deps.cfg.merge_override.model,
                effort=deps.cfg.merge_override.effort,
                stage="pre-merge",
            )
            parse(merger_result, AgentRole.MERGER)
            deps.git_svc.fast_forward_branch(
                deps.repo_root, target_branch, MERGE_SANDBOX
            )
        finally:
            try:
                deps.git_svc.remove_worktree(deps.repo_root, worktree_path)
            except Exception as exc:
                print(
                    f"Warning: could not remove merge worktree: {exc}", file=sys.stderr
                )
            try:
                deps.git_svc.delete_branch(MERGE_SANDBOX, deps.repo_root)
            except Exception as exc:
                print(
                    f"Warning: could not delete sandbox branch: {exc}", file=sys.stderr
                )
        deps.status_display.print("\nBranches merged.")
        _delete_merged_branches(
            [branch_for(i["number"]) for i in conflict_issues], deps
        )
        for issue in conflict_issues:
            deps.github_svc.close_issue(issue["number"])
        deps.github_svc.close_completed_parent_issues()

    return MergeResult(clean=clean_issues, conflicts=conflict_issues)
