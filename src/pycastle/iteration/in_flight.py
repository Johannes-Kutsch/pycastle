from collections.abc import Sequence
from pathlib import Path

from ..agents.output_protocol import AgentRole
from ..infrastructure.worktree import worktree_name_for_branch, worktree_path
from ..services import GitService
from ..session import RoleSession
from .implement import branch_for


def select_in_flight_issues(
    issues: Sequence[dict], *, repo_root: Path, git_svc: GitService
) -> list[dict]:
    return [
        issue
        for issue in issues
        if _issue_is_in_flight(issue, repo_root=repo_root, git_svc=git_svc)
    ]


def _issue_is_in_flight(issue: dict, *, repo_root: Path, git_svc: GitService) -> bool:
    branch = branch_for(issue["number"])
    issue_worktree = worktree_path(worktree_name_for_branch(branch), repo_root)
    if _has_resumable_role_session(issue_worktree):
        return True
    if not git_svc.verify_ref_exists(branch, repo_root):
        return False
    return git_svc.branch_has_commits_ahead_of_merge_base(repo_root, branch)


def _has_resumable_role_session(worktree: Path) -> bool:
    return any(RoleSession(worktree, role).is_resumable() for role in AgentRole)
