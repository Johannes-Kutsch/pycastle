from pathlib import Path
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import AgentRole
from pycastle.iteration.in_flight import select_in_flight_issues
from pycastle.services import GitService
from pycastle.session.resume import SESSION_DIR_NAME


def test_select_in_flight_issues_keeps_input_order_across_mixed_evidence(
    tmp_path: Path,
):
    git_svc = MagicMock(spec=GitService)
    repo_root = tmp_path
    issues = [
        {"number": 1, "title": "Resume from role session"},
        {"number": 2, "title": "Resume from branch commits"},
        {"number": 3, "title": "Fresh planning candidate"},
    ]

    role_dir = (
        repo_root
        / "pycastle"
        / ".worktrees"
        / "issue-1"
        / SESSION_DIR_NAME
        / AgentRole.IMPLEMENTER.value
    )
    role_dir.mkdir(parents=True)

    git_svc.verify_ref_exists.side_effect = lambda ref, _repo_root: (
        ref == "pycastle/issue-2"
    )
    git_svc.branch_has_commits_ahead_of_merge_base.side_effect = (
        lambda _repo_root, branch, main_branch="main": branch == "pycastle/issue-2"
    )

    assert select_in_flight_issues(issues, repo_root=repo_root, git_svc=git_svc) == [
        issues[0],
        issues[1],
    ]
