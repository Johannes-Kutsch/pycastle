import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.config import Config
from pycastle.agents.output_protocol import AgentRole
from pycastle.infrastructure.worktree import worktree_identity
from pycastle.iteration.in_flight import select_in_flight_issues
from pycastle.iteration.implement import branch_for
from pycastle.services import GitService
from pycastle.session.resume import RoleSession, SESSION_DIR_NAME


def _commit(repo_root: Path, message: str, content: str) -> None:
    (repo_root / "tracked.txt").write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_root), "add", "tracked.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-m", message],
        check=True,
        capture_output=True,
    )


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
        / "claude"
    )
    role_dir.mkdir(parents=True)
    (role_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

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


def test_select_in_flight_issues_uses_only_resumable_role_session_worktree_evidence(
    tmp_path: Path,
):
    git_svc = MagicMock(spec=GitService)
    repo_root = tmp_path
    issues = [
        {
            "number": 1,
            "title": "Resume from started role session",
            "body": "Issue body 1",
            "comments": [{"author": "alice", "body": "keep context"}],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
        {
            "number": 2,
            "title": "Empty leftover role dir",
            "body": "Issue body 2",
            "comments": [{"author": "bob", "body": "leftover"}],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
        {
            "number": 3,
            "title": "No evidence",
            "body": "Issue body 3",
            "comments": [{"author": "carol", "body": "fresh"}],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
    ]

    started_role_dir = (
        repo_root
        / "pycastle"
        / ".worktrees"
        / "issue-1"
        / SESSION_DIR_NAME
        / AgentRole.IMPLEMENTER.value
        / "claude"
    )
    started_role_dir.mkdir(parents=True)
    (started_role_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    empty_role_dir = (
        repo_root
        / "pycastle"
        / ".worktrees"
        / "issue-2"
        / SESSION_DIR_NAME
        / AgentRole.IMPLEMENTER.value
    )
    empty_role_dir.mkdir(parents=True)

    git_svc.verify_ref_exists.return_value = False

    result = select_in_flight_issues(issues, repo_root=repo_root, git_svc=git_svc)

    assert result == [issues[0]]
    assert result[0] is issues[0]


@pytest.mark.parametrize("role", list(AgentRole))
def test_select_in_flight_issues_treats_any_role_session_dir_under_issue_worktree_as_in_flight(
    tmp_path: Path,
    role: AgentRole,
):
    git_svc = MagicMock(spec=GitService)
    issue_number = 7
    issue = {
        "number": issue_number,
        "title": "Resume from branch-owned worktree role session",
        "labels": ["behavior-slice"],
    }
    issue_worktree = worktree_identity(branch_for(issue_number), tmp_path).path
    role_dir = issue_worktree / SESSION_DIR_NAME / role.value / "claude"
    role_dir.mkdir(parents=True)
    (role_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    git_svc.verify_ref_exists.return_value = False

    assert select_in_flight_issues([issue], repo_root=tmp_path, git_svc=git_svc) == [
        issue
    ]


def test_select_in_flight_issues_omits_metadata_only_role_session_without_branch_evidence(
    tmp_path: Path,
):
    git_svc = MagicMock(spec=GitService)
    issue = {
        "number": 1,
        "title": "Done role session keeps only metadata",
        "body": "Issue body 1",
        "comments": [{"author": "alice", "body": "done"}],
        "labels": ["ready-for-agent", "behavior-slice"],
    }
    role_session = RoleSession(
        tmp_path / "pycastle" / ".worktrees" / "issue-1",
        AgentRole.IMPLEMENTER,
    )
    role_session.start_fresh()
    role_session.save_service_session_metadata("claude", "thread-123")

    git_svc.verify_ref_exists.return_value = False

    assert select_in_flight_issues([issue], repo_root=tmp_path, git_svc=git_svc) == []


def test_select_in_flight_issues_returns_exact_issue_for_branch_with_commits_ahead(
    git_repo: Path,
):
    issues = [
        {"number": 1, "title": "Branch-backed in-flight", "labels": ["behavior-slice"]},
        {"number": 2, "title": "No branch evidence", "labels": ["behavior-slice"]},
    ]
    git_svc = GitService(Config())

    _commit(git_repo, "seed main", "main-1\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-b", "pycastle/issue-1"],
        check=True,
        capture_output=True,
    )
    _commit(git_repo, "issue work", "main-1\nissue-1\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    _commit(git_repo, "advance main", "main-1\nmain-2\n")

    assert select_in_flight_issues(issues, repo_root=git_repo, git_svc=git_svc) == [
        issues[0]
    ]


def test_select_in_flight_issues_omits_leftover_empty_branch_ref(git_repo: Path):
    issues = [
        {"number": 1, "title": "Leftover ref", "labels": ["behavior-slice"]},
    ]
    git_svc = GitService(Config())

    _commit(git_repo, "seed main", "main-1\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "pycastle/issue-1"],
        check=True,
        capture_output=True,
    )

    assert select_in_flight_issues(issues, repo_root=git_repo, git_svc=git_svc) == []


def test_select_in_flight_issues_omits_issue_without_worktree_or_branch(
    tmp_path: Path,
):
    git_svc = MagicMock(spec=GitService)
    issues = [
        {"number": 1, "title": "No evidence", "labels": ["behavior-slice"]},
    ]

    git_svc.verify_ref_exists.return_value = False

    assert select_in_flight_issues(issues, repo_root=tmp_path, git_svc=git_svc) == []


def test_select_in_flight_issues_keeps_ready_issue_input_order_for_branch_evidence(
    git_repo: Path,
):
    issues = [
        {
            "number": 3,
            "title": "Third issue first in input",
            "labels": ["behavior-slice"],
        },
        {
            "number": 1,
            "title": "First issue second in input",
            "labels": ["behavior-slice"],
        },
        {"number": 2, "title": "No branch evidence", "labels": ["behavior-slice"]},
    ]
    git_svc = GitService(Config())

    _commit(git_repo, "seed main", "main-1\n")

    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-b", "pycastle/issue-1"],
        check=True,
        capture_output=True,
    )
    _commit(git_repo, "issue 1 work", "main-1\nissue-1\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    _commit(git_repo, "advance main", "main-1\nmain-2\n")

    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-b", "pycastle/issue-3"],
        check=True,
        capture_output=True,
    )
    _commit(git_repo, "issue 3 work", "main-1\nmain-2\nissue-3\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    assert select_in_flight_issues(issues, repo_root=git_repo, git_svc=git_svc) == [
        issues[0],
        issues[1],
    ]
