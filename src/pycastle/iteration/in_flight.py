from collections.abc import Sequence
from pathlib import Path

from pycastle.agents.output_protocol import AgentRole
from pycastle.infrastructure.worktree import worktree_identity
from pycastle.iteration.implement import branch_for
from pycastle.services import GitService
from pycastle.session import RoleSession


def select_in_flight_issues(
    issues: Sequence[dict],
    *,
    repo_root: Path,
    git_svc: GitService,
    operating_branch: str = "main",
) -> list[dict]:
    return [
        issue
        for issue in issues
        if _issue_is_in_flight(
            issue,
            repo_root=repo_root,
            git_svc=git_svc,
            operating_branch=operating_branch,
        )
    ]


def _issue_is_in_flight(
    issue: dict,
    *,
    repo_root: Path,
    git_svc: GitService,
    operating_branch: str = "main",
) -> bool:
    branch = branch_for(issue["number"])
    issue_worktree = worktree_identity(branch, repo_root).path
    if _has_resumable_role_session(issue_worktree):
        return True
    if not git_svc.verify_ref_exists(branch, repo_root):
        return False
    return git_svc.branch_has_commits_ahead_of_merge_base(
        repo_root, branch, operating_branch
    )


def _has_resumable_role_session(worktree: Path) -> bool:
    return any(RoleSession(worktree, role).is_resumable() for role in AgentRole)
