import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pycastle.agent_output_protocol import CompletionOutput, PromiseParseError
from pycastle.agent_runner import RunRequest
from pycastle.config import Config
from pycastle.services import GitCommandError, GitService
from pycastle.services import GithubService
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
)
from pycastle.status_display import PlainStatusDisplay
from pycastle.iteration.merge import merge_phase


@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.is_working_tree_clean.return_value = True
    svc.try_merge.return_value = True
    svc.is_ancestor.return_value = True
    svc.get_current_branch.return_value = "main"
    svc.list_worktrees.return_value = []
    svc.verify_ref_exists.return_value = False

    def _fake_create_worktree(repo, wt, branch, sha=None):
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    svc.create_worktree.side_effect = _fake_create_worktree
    return svc


@pytest.fixture
def github_svc():
    return MagicMock(spec=GithubService)


@pytest.fixture
def agent_runner():
    return FakeAgentRunner([CompletionOutput()] * 10)


@pytest.fixture
def deps(tmp_path, git_svc, github_svc, agent_runner):
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=agent_runner,
        cfg=Config(),
        logger=RecordingLogger(),
        status_display=PlainStatusDisplay(),
    )


def _run(completed, deps):
    return asyncio.run(merge_phase(completed, deps))


def _make_deps(tmp_path, git_svc, github_svc, agent_runner):
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=agent_runner,
        cfg=Config(),
        logger=RecordingLogger(),
        status_display=PlainStatusDisplay(),
    )


# ── Clean merge path ──────────────────────────────────────────────────────────


def test_clean_merge_returns_all_issues_as_clean(deps):
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    result = _run(issues, deps)
    assert result.clean == issues
    assert result.conflicts == []


def test_clean_merge_closes_each_issue(deps, github_svc):
    issues = [{"number": 7, "title": "Fix A"}, {"number": 8, "title": "Fix B"}]
    _run(issues, deps)
    closed = [call.args[0] for call in github_svc.close_issue.call_args_list]
    assert sorted(closed) == [7, 8]


def test_clean_merge_calls_close_completed_parent_issues_once(deps, github_svc):
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    _run(issues, deps)
    assert github_svc.close_completed_parent_issues.call_count == 1


def test_clean_merge_does_not_spawn_merger(deps, agent_runner):
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    _run(issues, deps)
    assert agent_runner.calls == []


def test_clean_merge_deletes_merged_branches(deps, git_svc):
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    _run(issues, deps)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-1" in deleted
    assert "pycastle/issue-2" in deleted


# ── Conflict path ─────────────────────────────────────────────────────────────


def _conflict_on(issue_numbers: list[int]):
    conflict_set = set(issue_numbers)

    def _side_effect(repo_path, branch):
        result = not any(f"issue-{n}" in branch for n in conflict_set)
        return result

    return _side_effect


def test_conflict_branch_is_in_conflicts(deps, git_svc):
    git_svc.try_merge.side_effect = _conflict_on([2])
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    result = _run(issues, deps)
    assert result.conflicts == [{"number": 2, "title": "Conflict"}]
    assert result.clean == [{"number": 1, "title": "Clean"}]


def test_conflict_spawns_merger_with_conflict_branches_only(
    deps, git_svc, agent_runner
):
    git_svc.try_merge.side_effect = _conflict_on([2])
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, deps)
    merger_calls = [c for c in agent_runner.calls if c.name == "Merge Agent"]
    assert len(merger_calls) == 1
    branches_arg = merger_calls[0].prompt_args["BRANCHES"]
    assert "pycastle/issue-2" in branches_arg
    assert "pycastle/issue-1" not in branches_arg


def test_conflict_closes_conflict_issue_after_merger(deps, git_svc, github_svc):
    git_svc.try_merge.side_effect = _conflict_on([2])
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, deps)
    closed = [call.args[0] for call in github_svc.close_issue.call_args_list]
    assert 2 in closed


def test_conflict_calls_close_completed_parent_issues(deps, git_svc, github_svc):
    git_svc.try_merge.side_effect = _conflict_on([1])
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    assert github_svc.close_completed_parent_issues.call_count == 1


def test_conflict_deletes_sandbox_branch(deps, git_svc):
    git_svc.try_merge.side_effect = _conflict_on([1])
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/merge-sandbox" in deleted


def test_conflict_deletes_conflict_branch_after_merger(deps, git_svc):
    git_svc.try_merge.side_effect = _conflict_on([2])
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, deps)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-2" in deleted


def test_multiple_conflict_issues_all_closed(deps, git_svc, github_svc):
    git_svc.try_merge.return_value = False
    issues = [
        {"number": 10, "title": "A"},
        {"number": 11, "title": "B"},
        {"number": 12, "title": "C"},
    ]
    _run(issues, deps)
    closed = [call.args[0] for call in github_svc.close_issue.call_args_list]
    assert sorted(closed) == [10, 11, 12]
    assert github_svc.close_completed_parent_issues.call_count == 1


def test_merger_does_not_receive_issues_prompt_arg(deps, git_svc, agent_runner):
    git_svc.try_merge.return_value = False
    issues = [{"number": 3, "title": "Conflict"}]
    _run(issues, deps)
    merger_calls = [c for c in agent_runner.calls if c.name == "Merge Agent"]
    assert len(merger_calls) == 1
    assert "ISSUES" not in merger_calls[0].prompt_args


# ── Branch deletion edge cases ────────────────────────────────────────────────


def test_non_ancestor_branch_not_deleted(deps, git_svc):
    git_svc.is_ancestor.return_value = False
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    git_svc.delete_branch.assert_not_called()


def test_branch_deletion_error_does_not_abort_merge(deps, git_svc):
    git_svc.delete_branch.side_effect = [
        GitCommandError("fail", returncode=1, stderr=""),
        None,
    ]
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    result = _run(issues, deps)
    assert result.clean == issues


# ── Merger fast-forward behaviour ─────────────────────────────────────────────


def test_successful_merger_fast_forwards_target_branch(deps, git_svc):
    git_svc.try_merge.return_value = False
    git_svc.get_current_branch.return_value = "main"
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    git_svc.fast_forward_branch.assert_called_once_with(
        deps.repo_root, "main", "pycastle/merge-sandbox"
    )


def test_incomplete_merger_raises_and_does_not_fast_forward(
    tmp_path, git_svc, github_svc
):
    git_svc.try_merge.return_value = False
    fake = FakeAgentRunner([PromiseParseError("no <promise>COMPLETE</promise> tag")])
    local_deps = _make_deps(tmp_path, git_svc, github_svc, fake)
    issues = [{"number": 1, "title": "Conflict"}]
    with pytest.raises(PromiseParseError):
        _run(issues, local_deps)
    git_svc.fast_forward_branch.assert_not_called()


def test_preflight_failure_from_merger_raises_and_does_not_fast_forward(
    tmp_path, git_svc, github_svc
):
    from pycastle.agent_result import PreflightFailure

    git_svc.try_merge.return_value = False
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    fake = FakeAgentRunner([failure])
    local_deps = _make_deps(tmp_path, git_svc, github_svc, fake)
    issues = [{"number": 1, "title": "Conflict"}]
    with pytest.raises(RuntimeError, match="preflight"):
        _run(issues, local_deps)
    git_svc.fast_forward_branch.assert_not_called()


def test_preflight_failure_from_merger_still_removes_worktree(
    tmp_path, git_svc, github_svc
):
    from pycastle.agent_result import PreflightFailure

    git_svc.try_merge.return_value = False
    failure = PreflightFailure(failures=(("mypy", "mypy .", "error"),))
    fake = FakeAgentRunner([failure])
    local_deps = _make_deps(tmp_path, git_svc, github_svc, fake)
    issues = [{"number": 1, "title": "Conflict"}]
    with pytest.raises(RuntimeError):
        _run(issues, local_deps)
    expected_path = (
        local_deps.repo_root
        / local_deps.cfg.pycastle_dir
        / ".worktrees"
        / "merge-sandbox"
    )
    git_svc.remove_worktree.assert_called_once_with(local_deps.repo_root, expected_path)


# ── Exception safety ──────────────────────────────────────────────────────────


def test_sandbox_branch_deleted_when_run_agent_raises(tmp_path, git_svc, github_svc):
    git_svc.try_merge.return_value = False
    fake = FakeAgentRunner([RuntimeError("agent crashed")])
    local_deps = _make_deps(tmp_path, git_svc, github_svc, fake)
    issues = [{"number": 1, "title": "Conflict"}]
    with pytest.raises(RuntimeError, match="agent crashed"):
        _run(issues, local_deps)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/merge-sandbox" in deleted


# ── Worktree lifecycle ────────────────────────────────────────────────────────


def test_conflict_creates_worktree_at_merge_sandbox(deps, git_svc):
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    expected_path = (
        deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "merge-sandbox"
    )
    git_svc.create_worktree.assert_called_once_with(
        deps.repo_root,
        expected_path,
        "pycastle/merge-sandbox",
        git_svc.get_head_sha.return_value,
    )


def test_merger_receives_worktree_path_as_mount(deps, git_svc, agent_runner):
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    merger_calls = [c for c in agent_runner.calls if c.name == "Merge Agent"]
    assert len(merger_calls) == 1
    expected_path = (
        deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "merge-sandbox"
    )
    assert merger_calls[0].mount_path == expected_path


def test_worktree_removed_after_merger(deps, git_svc):
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    expected_path = (
        deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "merge-sandbox"
    )
    git_svc.remove_worktree.assert_called_once_with(deps.repo_root, expected_path)


def test_worktree_removed_when_run_agent_raises(tmp_path, git_svc, github_svc):
    git_svc.try_merge.return_value = False
    fake = FakeAgentRunner([RuntimeError("agent crashed")])
    local_deps = _make_deps(tmp_path, git_svc, github_svc, fake)
    issues = [{"number": 1, "title": "Conflict"}]
    with pytest.raises(RuntimeError, match="agent crashed"):
        _run(issues, local_deps)
    expected_path = (
        local_deps.repo_root
        / local_deps.cfg.pycastle_dir
        / ".worktrees"
        / "merge-sandbox"
    )
    git_svc.remove_worktree.assert_called_once_with(local_deps.repo_root, expected_path)


# ── Empty input ───────────────────────────────────────────────────────────────


def test_empty_completed_list_returns_empty_result(deps, github_svc, agent_runner):
    result = _run([], deps)
    assert result.clean == []
    assert result.conflicts == []
    github_svc.close_issue.assert_not_called()
    github_svc.close_completed_parent_issues.assert_not_called()
    assert agent_runner.calls == []


# ── Active worktree removal before branch deletion ────────────────────────────


def test_active_worktree_removed_when_merged_branch_is_cleaned_up(deps, git_svc):
    worktree_path = deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "issue-1"
    git_svc.list_worktrees.return_value = [worktree_path]
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    git_svc.remove_worktree.assert_called_once_with(deps.repo_root, worktree_path)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-1" in deleted


def test_worktree_unregistered_before_branch_deletion(deps, git_svc):
    worktree_path = deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "issue-1"
    git_svc.list_worktrees.return_value = [worktree_path]
    call_order: list[str] = []
    git_svc.remove_worktree.side_effect = lambda *a, **kw: call_order.append("remove")
    git_svc.delete_branch.side_effect = lambda *a, **kw: call_order.append("delete")
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    assert call_order.index("remove") < call_order.index("delete")


def test_merged_branch_without_active_worktree_is_deleted_without_worktree_removal(
    deps, git_svc
):
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    git_svc.remove_worktree.assert_not_called()


def test_worktree_removal_failure_does_not_abort_branch_deletion(deps, git_svc):
    worktree_path = deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "issue-1"
    git_svc.list_worktrees.return_value = [worktree_path]
    git_svc.remove_worktree.side_effect = RuntimeError("disk full")
    issues = [{"number": 1, "title": "Fix A"}]
    result = _run(issues, deps)
    git_svc.delete_branch.assert_called()
    assert result.clean == issues


# ── StatusDisplay routing ─────────────────────────────────────────────────────


@pytest.fixture
def recording_deps(tmp_path, git_svc, github_svc, agent_runner):
    recording = RecordingStatusDisplay()
    return (
        Deps(
            env={},
            repo_root=tmp_path,
            git_svc=git_svc,
            github_svc=github_svc,
            agent_runner=agent_runner,
            cfg=Config(),
            logger=RecordingLogger(),
            status_display=recording,
        ),
        recording,
    )


def test_merge_phase_routes_deleted_branch_through_status_display(
    recording_deps, git_svc, capsys
):
    """merge_phase must route 'Deleted merged branch' through status_display.print()."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("Deleted merged branch" in msg for msg in print_messages)
    assert "Deleted merged branch" not in capsys.readouterr().out


def test_merge_phase_routes_branches_merged_through_status_display(
    recording_deps, git_svc, capsys
):
    """merge_phase must route 'Branches merged' through status_display.print() after conflict resolution."""
    deps, recording = recording_deps
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("Branches merged" in msg for msg in print_messages)
    assert "Branches merged" not in capsys.readouterr().out


def test_merge_phase_routes_dirty_tree_message_through_status_display(
    recording_deps, git_svc, capsys
):
    """merge_phase must route the dirty-tree wait message through status_display.print()."""
    deps, recording = recording_deps
    git_svc.is_working_tree_clean.side_effect = [False, True]
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("Working tree" in msg for msg in print_messages)
    assert "Working tree" not in capsys.readouterr().out


def test_merge_phase_dirty_tree_message_uses_error_style(recording_deps, git_svc):
    """The dirty-tree wait message must use style='error' and contain no [red] markup."""
    deps, recording = recording_deps
    git_svc.is_working_tree_clean.side_effect = [False, True]
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    dirty_calls = [
        c for c in recording.calls if c[0] == "print" and "Working tree" in str(c[2])
    ]
    assert dirty_calls, "Dirty-tree message must be printed"
    for call in dirty_calls:
        assert call[1] == "", (
            f"Dirty-tree message must use anonymous caller; got {call[1]!r}"
        )
        assert call[3] == "error", (
            f"Dirty-tree message must use style='error'; got {call[3]!r}"
        )
        assert "[red]" not in str(call[2]), (
            f"Message must not contain [red] markup: {call[2]!r}"
        )


def test_merge_phase_dirty_tree_message_references_merge_phase(recording_deps, git_svc):
    """The dirty-tree wait message must name the merge phase, not another phase."""
    deps, recording = recording_deps
    git_svc.is_working_tree_clean.side_effect = [False, True]
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    dirty_msg = next((msg for msg in print_messages if "Working tree" in msg), None)
    assert dirty_msg is not None
    assert "merge" in dirty_msg


def test_merge_phase_does_not_print_dirty_tree_message_when_working_tree_is_clean(
    recording_deps, git_svc
):
    """merge_phase must not print a dirty-tree message when the working tree is already clean."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert not any("Working tree" in msg for msg in print_messages)


def test_merge_phase_completes_normally_after_polling_through_multiple_dirty_states(
    recording_deps, git_svc
):
    """merge_phase must complete normally when the working tree becomes clean after multiple polls."""
    deps, recording = recording_deps
    git_svc.is_working_tree_clean.side_effect = [False, False, True]
    issues = [{"number": 1, "title": "Fix A"}]
    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        result = _run(issues, deps)
    assert result.clean == issues
    assert result.conflicts == []


def test_merge_phase_polls_dirty_tree_every_10_seconds(recording_deps, git_svc):
    """merge_phase must sleep exactly 10 s between dirty-tree polls."""
    deps, recording = recording_deps
    # Initial: dirty → print; loop: dirty → sleep, dirty → sleep, clean → exit
    git_svc.is_working_tree_clean.side_effect = [False, False, False, True]
    issues = [{"number": 1, "title": "Fix A"}]
    with patch(
        "pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        _run(issues, deps)
    assert mock_sleep.call_count == 2
    assert all(call.args[0] == 10 for call in mock_sleep.call_args_list)


# ── Merge status row ──────────────────────────────────────────────────────────


def test_merge_row_added_at_start_of_merge_phase(recording_deps):
    """merge_phase must add a 'merge' status row with 'Merging' phase label."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    assert ("register", "Merge", "started", "Merging") in recording.calls


def test_merge_row_removed_after_clean_merges(recording_deps):
    """merge_phase must remove the 'merge' row once programmatic merges complete."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    assert ("remove", "Merge", "finished", "success") in recording.calls


def test_merge_row_removed_when_completed_is_empty(recording_deps):
    """merge_phase must remove the 'Merge' row even when there is nothing to merge."""
    deps, recording = recording_deps
    _run([], deps)
    assert ("remove", "Merge", "finished", "success") in recording.calls


def test_merge_row_removed_before_merger_spawned(tmp_path, git_svc, github_svc):
    """merge_phase must remove the 'Merge' row before spawning the Merge Agent."""
    recording = RecordingStatusDisplay()
    removed_when_merger_ran: list[bool] = []

    async def side_effect(request: RunRequest):
        if request.name == "Merge Agent":
            removed_when_merger_ran.append(
                ("remove", "Merge", "finished", "success") in recording.calls
            )
        return CompletionOutput()

    agent_runner = FakeAgentRunner(side_effect=side_effect)
    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=agent_runner,
        cfg=Config(),
        logger=RecordingLogger(),
        status_display=recording,
    )
    git_svc.try_merge.return_value = False
    _run([{"number": 1, "title": "Conflict"}], deps)
    assert removed_when_merger_ran == [True]


def test_merge_row_removed_with_failed_style_when_exception_raised(
    recording_deps, git_svc
):
    """merge_phase must remove the 'Merge' row with 'failed' style when an exception occurs."""
    deps, recording = recording_deps
    git_svc.try_merge.side_effect = GitCommandError(
        "merge exploded", returncode=1, stderr=""
    )

    with pytest.raises(GitCommandError):
        _run([{"number": 1, "title": "Fix A"}], deps)

    assert ("remove", "Merge", "failed", "error") in recording.calls


def test_merge_row_not_removed_with_failed_style_after_row_already_removed(
    tmp_path, git_svc, github_svc
):
    """The 'Merge' row must not get a second failed-style remove when the exception fires after the row was already removed."""
    from pycastle.agent_result import PreflightFailure

    recording = RecordingStatusDisplay()
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    deps = Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=FakeAgentRunner([failure]),
        cfg=Config(),
        logger=RecordingLogger(),
        status_display=recording,
    )
    git_svc.try_merge.return_value = False

    with pytest.raises(RuntimeError, match="preflight"):
        asyncio.run(merge_phase([{"number": 1, "title": "Conflict"}], deps))

    assert ("remove", "Merge", "failed", "error") not in recording.calls


# ── Merger work_body ──────────────────────────────────────────────────────────


def test_merger_run_call_passes_work_body_with_conflict_count(
    tmp_path, git_svc, github_svc
):
    git_svc.try_merge.return_value = False
    recording_runner = FakeAgentRunner([CompletionOutput()])
    deps = dataclasses.replace(
        _make_deps(tmp_path, git_svc, github_svc, recording_runner),
        status_display=PlainStatusDisplay(),
    )
    conflict_issues = [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}]

    _run(conflict_issues, deps)

    merger_calls = [c for c in recording_runner.calls if c.name == "Merge Agent"]
    assert len(merger_calls) == 1
    assert merger_calls[0].work_body == f"Merging {len(conflict_issues)} Branches"
