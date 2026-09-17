import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pycastle.iteration._merged_branch_teardown import teardown_merged_branch
from pycastle.services import GitCommandError
from tests.support import RecordingStatusDisplay, functional_git_svc


def _run(branch, deps, *, on_progress=None):
    return asyncio.run(teardown_merged_branch(branch, deps, on_progress=on_progress))


def _make_deps(tmp_path, *, git_svc=None, is_ancestor=True):
    svc = git_svc or functional_git_svc(is_ancestor=is_ancestor)
    svc.list_worktrees.side_effect = None
    svc.list_worktrees.return_value = []

    cfg = MagicMock()
    cfg.operating_branch = "main"

    deps = MagicMock()
    deps.git_svc = svc
    deps.repo_root = tmp_path
    deps.cfg = cfg
    deps.status_display = RecordingStatusDisplay()
    return deps


# ── Ancestry check ────────────────────────────────────────────────────────────


def test_not_ancestor_returns_none(tmp_path):
    deps = _make_deps(tmp_path, is_ancestor=False)
    result = _run("pycastle/issue-1", deps)
    assert result is None


def test_not_ancestor_does_not_delete_branch(tmp_path):
    deps = _make_deps(tmp_path, is_ancestor=False)
    _run("pycastle/issue-1", deps)
    deps.git_svc.delete_branch.assert_not_called()


# ── Worktree not registered ───────────────────────────────────────────────────


def test_unregistered_worktree_skips_teardown_and_deletes_branch(tmp_path):
    deps = _make_deps(tmp_path)

    result = _run("pycastle/issue-1", deps)

    deps.git_svc.remove_worktree.assert_not_called()
    deps.git_svc.delete_branch.assert_called_once()
    assert result == "pycastle/issue-1"


# ── Worktree exists on disk but not registered ────────────────────────────────


def test_disk_only_worktree_triggers_cleanup(tmp_path):
    from pycastle.infrastructure.worktree import worktree_identity

    branch = "pycastle/issue-1"
    worktree_path = worktree_identity(branch, tmp_path).path
    worktree_path.mkdir(parents=True)

    deps = _make_deps(tmp_path)

    with patch(
        "pycastle.iteration._merged_branch_teardown.cleanup_durable_issue_worktree_after_success"
    ) as mock_cleanup:
        result = _run(branch, deps)

    mock_cleanup.assert_called_once()
    assert result == branch


# ── Worktree registered ───────────────────────────────────────────────────────


def test_registered_worktree_triggers_cleanup(tmp_path):
    from pycastle.infrastructure.worktree import worktree_identity

    branch = "pycastle/issue-1"
    worktree_path = worktree_identity(branch, tmp_path).path

    deps = _make_deps(tmp_path)
    deps.git_svc.list_worktrees.side_effect = None
    deps.git_svc.list_worktrees.return_value = [worktree_path]

    with patch(
        "pycastle.iteration._merged_branch_teardown.cleanup_durable_issue_worktree_after_success"
    ) as mock_cleanup:
        result = _run(branch, deps)

    mock_cleanup.assert_called_once()
    assert result == branch


# ── Worktree teardown failure — GitCommandError ───────────────────────────────


def test_worktree_git_error_emits_warning_and_continues_to_branch_delete(tmp_path):
    from pycastle.infrastructure.worktree import worktree_identity

    branch = "pycastle/issue-1"
    worktree_path = worktree_identity(branch, tmp_path).path

    deps = _make_deps(tmp_path)
    deps.git_svc.list_worktrees.return_value = [worktree_path]
    deps.git_svc.remove_worktree.side_effect = GitCommandError("remove failed")

    result = _run(branch, deps)

    warning_calls = [
        c
        for c in deps.status_display.calls
        if c[0] == "print" and "worktree" in str(c[2])
    ]
    assert len(warning_calls) == 1
    assert warning_calls[0][3] == "warning"
    deps.git_svc.delete_branch.assert_called_once()
    assert result == branch


# ── Worktree teardown failure — OSError ──────────────────────────────────────


def test_worktree_os_error_emits_warning_and_continues_to_branch_delete(tmp_path):
    from pycastle.infrastructure.worktree import worktree_identity

    branch = "pycastle/issue-1"
    worktree_path = worktree_identity(branch, tmp_path).path

    deps = _make_deps(tmp_path)
    deps.git_svc.list_worktrees.return_value = [worktree_path]
    deps.git_svc.remove_worktree.side_effect = OSError("perm denied")

    result = _run(branch, deps)

    warning_calls = [
        c
        for c in deps.status_display.calls
        if c[0] == "print" and "worktree" in str(c[2])
    ]
    assert len(warning_calls) == 1
    assert warning_calls[0][3] == "warning"
    deps.git_svc.delete_branch.assert_called_once()
    assert result == branch


# ── Worktree teardown failure — unexpected error ──────────────────────────────


def test_unexpected_worktree_error_propagates(tmp_path):
    from pycastle.infrastructure.worktree import worktree_identity

    branch = "pycastle/issue-1"
    worktree_path = worktree_identity(branch, tmp_path).path

    deps = _make_deps(tmp_path)
    deps.git_svc.list_worktrees.return_value = [worktree_path]
    deps.git_svc.remove_worktree.side_effect = ValueError("unexpected failure")

    with pytest.raises(ValueError, match="unexpected failure"):
        _run(branch, deps)


# ── Branch delete failure ─────────────────────────────────────────────────────


def test_branch_delete_git_error_emits_warning_and_returns_none(tmp_path):
    deps = _make_deps(tmp_path)
    deps.git_svc.delete_branch.side_effect = GitCommandError("delete failed")

    result = _run("pycastle/issue-1", deps)

    warning_calls = [
        c
        for c in deps.status_display.calls
        if c[0] == "print" and "branch" in str(c[2])
    ]
    assert len(warning_calls) == 1
    assert warning_calls[0][3] == "warning"
    assert result is None


# ── on_progress callback ──────────────────────────────────────────────────────


def test_on_progress_called_once_on_success(tmp_path):
    deps = _make_deps(tmp_path)
    count = [0]
    _run(
        "pycastle/issue-1", deps, on_progress=lambda: count.__setitem__(0, count[0] + 1)
    )
    assert count[0] == 1


def test_on_progress_called_once_when_not_ancestor(tmp_path):
    deps = _make_deps(tmp_path, is_ancestor=False)
    count = [0]
    _run(
        "pycastle/issue-1", deps, on_progress=lambda: count.__setitem__(0, count[0] + 1)
    )
    assert count[0] == 1


def test_on_progress_called_once_on_branch_delete_failure(tmp_path):
    deps = _make_deps(tmp_path)
    deps.git_svc.delete_branch.side_effect = GitCommandError("delete failed")
    count = [0]
    _run(
        "pycastle/issue-1", deps, on_progress=lambda: count.__setitem__(0, count[0] + 1)
    )
    assert count[0] == 1
