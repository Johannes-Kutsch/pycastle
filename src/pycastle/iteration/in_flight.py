from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ..infrastructure.worktree import worktree_name_for_branch, worktree_path
from ..session import any_role_dir_present
from .implement import branch_for


class InFlightGit(Protocol):
    def verify_ref_exists(self, ref: str, repo_path: Path) -> bool: ...

    def branch_has_commits_ahead_of_merge_base(
        self, repo_path: Path, branch: str, main_branch: str = "main"
    ) -> bool: ...


def select_in_flight_issues(
    issues: Sequence[dict], *, repo_root: Path, git_svc: InFlightGit
) -> list[dict]:
    return [
        issue
        for issue in issues
        if _issue_is_in_flight(issue, repo_root=repo_root, git_svc=git_svc)
    ]


def _issue_is_in_flight(issue: dict, *, repo_root: Path, git_svc: InFlightGit) -> bool:
    branch = branch_for(issue["number"])
    issue_worktree = worktree_path(worktree_name_for_branch(branch), repo_root)
    if any_role_dir_present(issue_worktree):
        return True
    if not git_svc.verify_ref_exists(branch, repo_root):
        return False
    return git_svc.branch_has_commits_ahead_of_merge_base(repo_root, branch)
