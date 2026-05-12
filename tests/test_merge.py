import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pycastle.agent_output_protocol import CompletionOutput, PromiseParseError
from pycastle.agent_runner import RunRequest
from pycastle.config import Config
from pycastle.services import GitCommandError, GitService
from pycastle.services import GithubAPIError, GithubService
from pycastle.iteration._deps import (
    FakeAgentRunner,
    RecordingStatusDisplay,
    StubPreflightCache,
    _make_deps,
)
from pycastle.iteration.merge import MergeResult, merge_phase
from pycastle.iteration.preflight import PreflightAFK, PreflightHITL, PreflightReady


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
    return _make_deps(tmp_path, agent_runner, git_svc=git_svc, github_svc=github_svc)


def _run(completed, deps):
    return asyncio.run(merge_phase(completed, deps))


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
    branches_arg = merger_calls[0].scope_args["BRANCHES"]
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
    assert "ISSUES" not in merger_calls[0].scope_args


# ── Branch deletion edge cases ────────────────────────────────────────────────


def test_non_ancestor_branch_not_deleted(deps, git_svc):
    git_svc.is_ancestor.return_value = False
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    git_svc.delete_branch.assert_not_called()


def test_non_ancestor_branch_skipped_while_ancestor_is_deleted(deps, git_svc):
    def _is_ancestor(branch, repo_root):
        return "issue-1" in branch

    git_svc.is_ancestor.side_effect = _is_ancestor
    issues = [
        {"number": 1, "title": "Ancestor"},
        {"number": 2, "title": "Non-ancestor"},
    ]
    _run(issues, deps)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-1" in deleted
    assert "pycastle/issue-2" not in deleted


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
    local_deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    issues = [{"number": 1, "title": "Conflict"}]
    with pytest.raises(PromiseParseError):
        _run(issues, local_deps)
    git_svc.fast_forward_branch.assert_not_called()


# ── Merge-time preflight via cache ───────────────────────────────────────────


def _make_preflight_skip_deps(tmp_path, git_svc, github_svc, verdict):

    git_svc.try_merge.return_value = False
    cache = StubPreflightCache(verdict)
    return _make_deps(
        tmp_path,
        FakeAgentRunner([]),
        git_svc=git_svc,
        github_svc=github_svc,
        preflight_cache=cache,
    )


def test_merge_phase_calls_get_safe_sha_when_conflicts_remain(
    tmp_path, git_svc, github_svc
):

    git_svc.try_merge.side_effect = _conflict_on([2])
    cache = StubPreflightCache(PreflightReady(sha="abc123"))
    cache.get_safe_sha = AsyncMock(return_value=PreflightReady(sha="abc123"))
    local_deps = _make_deps(
        tmp_path,
        FakeAgentRunner([CompletionOutput()]),
        git_svc=git_svc,
        github_svc=github_svc,
        preflight_cache=cache,
    )
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, local_deps)
    cache.get_safe_sha.assert_called_once()


def test_preflight_afk_returns_soft_skip_merge_result(tmp_path, git_svc, github_svc):

    verdict = PreflightAFK(sha="abc123", issue_number=99)
    local_deps = _make_preflight_skip_deps(tmp_path, git_svc, github_svc, verdict)
    issues = [{"number": 1, "title": "Conflict"}]
    result = _run(issues, local_deps)
    assert isinstance(result, MergeResult)
    assert result.conflicts == [{"number": 1, "title": "Conflict"}]
    assert result.clean == []


def test_preflight_hitl_returns_soft_skip_merge_result(tmp_path, git_svc, github_svc):

    verdict = PreflightHITL(sha="abc123", issue_number=42)
    local_deps = _make_preflight_skip_deps(tmp_path, git_svc, github_svc, verdict)
    issues = [{"number": 1, "title": "Conflict"}]
    result = _run(issues, local_deps)
    assert isinstance(result, MergeResult)
    assert result.conflicts == [{"number": 1, "title": "Conflict"}]
    assert result.clean == []


def test_preflight_skip_does_not_spawn_merger(tmp_path, git_svc, github_svc):

    verdict = PreflightAFK(sha="abc123", issue_number=99)
    git_svc.try_merge.return_value = False

    cache = StubPreflightCache(verdict)
    fake = FakeAgentRunner([])
    local_deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=github_svc,
        preflight_cache=cache,
    )
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, local_deps)
    assert fake.calls == []


def test_preflight_skip_does_not_fast_forward(tmp_path, git_svc, github_svc):

    verdict = PreflightAFK(sha="abc123", issue_number=99)
    local_deps = _make_preflight_skip_deps(tmp_path, git_svc, github_svc, verdict)
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, local_deps)
    local_deps.git_svc.fast_forward_branch.assert_not_called()


def test_preflight_skip_prints_merge_caller_message(tmp_path, git_svc, github_svc):

    verdict = PreflightAFK(sha="abc123", issue_number=99)
    recording = RecordingStatusDisplay()

    git_svc.try_merge.return_value = False
    local_deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            FakeAgentRunner([]),
            git_svc=git_svc,
            github_svc=github_svc,
            preflight_cache=StubPreflightCache(verdict),
        ),
        status_display=recording,
    )
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, local_deps)
    preflight_prints = [
        c
        for c in recording.calls
        if c[0] == "print" and "preflight" in str(c[2]).lower()
    ]
    assert preflight_prints, "expected a preflight skip message"
    assert all(c[1] == "Merge" for c in preflight_prints)


def test_preflight_skip_separates_clean_and_conflict_issues(
    tmp_path, git_svc, github_svc
):

    verdict = PreflightAFK(sha="abc123", issue_number=99)
    git_svc.try_merge.side_effect = _conflict_on([2])

    local_deps = _make_deps(
        tmp_path,
        FakeAgentRunner([]),
        git_svc=git_svc,
        github_svc=github_svc,
        preflight_cache=StubPreflightCache(verdict),
    )
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    result = _run(issues, local_deps)
    assert result.clean == [{"number": 1, "title": "Clean"}]
    assert result.conflicts == [{"number": 2, "title": "Conflict"}]


def test_preflight_skip_closes_parent_issues_for_clean_issues(
    tmp_path, git_svc, github_svc
):

    verdict = PreflightAFK(sha="abc123", issue_number=99)
    git_svc.try_merge.side_effect = _conflict_on([2])

    local_deps = _make_deps(
        tmp_path,
        FakeAgentRunner([]),
        git_svc=git_svc,
        github_svc=github_svc,
        preflight_cache=StubPreflightCache(verdict),
    )
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, local_deps)
    local_deps.github_svc.close_completed_parent_issues.assert_called_once()


# ── Exception safety ──────────────────────────────────────────────────────────


def test_sandbox_branch_deleted_when_run_agent_raises(tmp_path, git_svc, github_svc):
    git_svc.try_merge.return_value = False
    fake = FakeAgentRunner([RuntimeError("agent crashed")])
    local_deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
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
        "abc123",
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
    local_deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
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
        _make_deps(
            tmp_path,
            agent_runner,
            git_svc=git_svc,
            github_svc=github_svc,
            status_display=recording,
        ),
        recording,
    )


def test_merge_phase_routes_deleted_branch_through_status_display(
    recording_deps, git_svc, capsys
):
    """merge_phase must route 'Deleted merged branch' via the Merge row close summary."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    remove_messages = [
        c[2] for c in recording.calls if c[0] == "remove" and c[1] == "Merge"
    ]
    assert any("Deleted merged branch" in msg for msg in remove_messages)
    assert "Deleted merged branch" not in capsys.readouterr().out


def test_merge_phase_close_summary_lists_conflict_deleted_branches(
    recording_deps, git_svc, capsys
):
    """After conflict resolution, the Merge row close summary must list all deleted branches."""
    deps, recording = recording_deps
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)

    remove_calls = [c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"]
    assert remove_calls, "Merge row must be removed"
    shutdown_msg = remove_calls[-1][2]
    assert "Execution complete" in shutdown_msg
    assert "pycastle/issue-1" in shutdown_msg
    assert "Branches merged" not in capsys.readouterr().out


def test_close_message_combines_clean_and_conflict_deleted_branches(
    recording_deps, git_svc
):
    """The Merge row close message must list both clean-merged and conflict-merged deleted branches."""
    deps, recording = recording_deps
    git_svc.try_merge.side_effect = _conflict_on([2])
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, deps)

    remove_calls = [c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"]
    assert remove_calls, "Merge row must be removed"
    shutdown_msg = remove_calls[-1][2]
    assert "pycastle/issue-1" in shutdown_msg
    assert "pycastle/issue-2" in shutdown_msg
    assert "2 branch(es) merged and deleted" in shutdown_msg


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
    """The dirty-tree wait message must use style='error', caller='Merge', and contain no [red] markup."""
    deps, recording = recording_deps
    git_svc.is_working_tree_clean.side_effect = [False, True]
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    dirty_calls = [
        c for c in recording.calls if c[0] == "print" and "Working tree" in str(c[2])
    ]
    assert dirty_calls, "Dirty-tree message must be printed"
    for call in dirty_calls:
        assert call[1] == "Merge", (
            f"Dirty-tree message must use caller='Merge'; got {call[1]!r}"
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


def test_dirty_tree_message_printed_once_across_multiple_polls(recording_deps, git_svc):
    """The 'Working tree has uncommitted changes' message must be printed exactly once, not once per poll."""
    deps, recording = recording_deps
    git_svc.is_working_tree_clean.side_effect = [False, False, False, True]
    issues = [{"number": 1, "title": "Fix A"}]
    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        _run(issues, deps)
    dirty_prints = [
        c for c in recording.calls if c[0] == "print" and "Working tree" in str(c[2])
    ]
    assert len(dirty_prints) == 1


# ── Merge status row ──────────────────────────────────────────────────────────


def test_merge_row_added_at_start_of_merge_phase(recording_deps):
    """merge_phase must add a 'merge' status row with 'Merging' phase label."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    assert ("register", "Merge", "phase", "started", "Merging") in recording.calls


def test_merge_row_removed_after_clean_merges(recording_deps):
    """merge_phase must remove the 'Merge' row with a branch-list summary after clean merges."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    remove_calls = [c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"]
    assert remove_calls, "Merge row must be removed"
    shutdown_msg = remove_calls[-1][2]
    assert "Execution complete" in shutdown_msg
    assert "pycastle/issue-1" in shutdown_msg
    assert remove_calls[-1][3] == "success"


def test_merge_row_removed_when_completed_is_empty(recording_deps):
    """merge_phase must remove the 'Merge' row even when there is nothing to merge."""
    deps, recording = recording_deps
    _run([], deps)
    remove_calls = [c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"]
    assert remove_calls, "Merge row must be removed"
    assert remove_calls[-1][3] == "success"


def test_close_message_shows_zero_branches_merged_when_no_issues(recording_deps):
    """When completed is empty, the Merge row close message reports 0 branches merged and deleted."""
    deps, recording = recording_deps
    _run([], deps)
    remove_calls = [c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"]
    assert remove_calls
    shutdown_msg = remove_calls[-1][2]
    assert "Execution complete" in shutdown_msg
    assert "0 branch(es) merged and deleted" in shutdown_msg


def test_merge_row_still_active_while_merger_runs(tmp_path, git_svc, github_svc):
    """The 'Merge' phase row must remain open (not yet closed) while the Merge Agent runs."""
    recording = RecordingStatusDisplay()
    row_open_when_merger_ran: list[bool] = []

    async def side_effect(request: RunRequest):
        if request.name == "Merge Agent":
            remove_calls = [
                c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"
            ]
            row_open_when_merger_ran.append(len(remove_calls) == 0)
        return CompletionOutput()

    agent_runner = FakeAgentRunner(side_effect=side_effect)
    deps = _make_deps(
        tmp_path,
        agent_runner,
        git_svc=git_svc,
        github_svc=github_svc,
        status_display=recording,
    )
    git_svc.try_merge.return_value = False
    _run([{"number": 1, "title": "Conflict"}], deps)
    assert row_open_when_merger_ran == [True]


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
    """The 'Merge' row must not get a second failed-style remove when the preflight gate blocks the merger."""

    recording = RecordingStatusDisplay()
    deps = _make_deps(
        tmp_path,
        FakeAgentRunner([]),
        git_svc=git_svc,
        github_svc=github_svc,
        status_display=recording,
        preflight_cache=StubPreflightCache(PreflightAFK(sha="abc123", issue_number=99)),
    )
    git_svc.try_merge.return_value = False

    asyncio.run(merge_phase([{"number": 1, "title": "Conflict"}], deps))

    assert ("remove", "Merge", "failed", "error") not in recording.calls


# ── Merger work_body ──────────────────────────────────────────────────────────


def test_merger_run_call_passes_work_body_with_conflict_count(
    tmp_path, git_svc, github_svc
):
    git_svc.try_merge.return_value = False
    recording_runner = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(
        tmp_path, recording_runner, git_svc=git_svc, github_svc=github_svc
    )
    conflict_issues = [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}]

    _run(conflict_issues, deps)

    merger_calls = [c for c in recording_runner.calls if c.name == "Merge Agent"]
    assert len(merger_calls) == 1
    assert merger_calls[0].work_body == f"Merging {len(conflict_issues)} Branches"


# ── auto_push wiring ──────────────────────────────────────────────────────────


def test_auto_push_calls_push_after_clean_merges(deps, git_svc):
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)
    git_svc.push.assert_called_once_with(deps.repo_root)


def test_auto_push_calls_push_after_merger_fast_forward(deps, git_svc):
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    git_svc.push.assert_called_once_with(deps.repo_root)


def test_auto_push_calls_push_in_preflight_skip_when_clean_issues_exist(
    tmp_path, git_svc, github_svc
):

    git_svc.try_merge.side_effect = _conflict_on([2])
    local_deps = _make_deps(
        tmp_path,
        FakeAgentRunner([]),
        git_svc=git_svc,
        github_svc=github_svc,
        preflight_cache=StubPreflightCache(PreflightAFK(sha="abc123", issue_number=99)),
    )
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, local_deps)
    local_deps.git_svc.push.assert_called_once_with(local_deps.repo_root)


def test_auto_push_does_not_call_push_in_preflight_skip_when_no_clean_issues(
    tmp_path, git_svc, github_svc
):

    git_svc.try_merge.return_value = False
    local_deps = _make_deps(
        tmp_path,
        FakeAgentRunner([]),
        git_svc=git_svc,
        github_svc=github_svc,
        preflight_cache=StubPreflightCache(PreflightAFK(sha="abc123", issue_number=99)),
    )
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, local_deps)
    local_deps.git_svc.push.assert_not_called()


def test_auto_push_false_does_not_call_push_on_clean_merge(deps, git_svc):
    local_deps = dataclasses.replace(deps, cfg=Config(auto_push=False))
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, local_deps)
    git_svc.push.assert_not_called()


def test_auto_push_false_does_not_call_push_on_conflict_path(deps, git_svc):
    git_svc.try_merge.return_value = False
    local_deps = dataclasses.replace(deps, cfg=Config(auto_push=False))
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, local_deps)
    git_svc.push.assert_not_called()


def test_auto_push_false_does_not_call_push_in_preflight_skip(
    tmp_path, git_svc, github_svc
):

    git_svc.try_merge.side_effect = _conflict_on([2])
    local_deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            FakeAgentRunner([]),
            git_svc=git_svc,
            github_svc=github_svc,
            preflight_cache=StubPreflightCache(
                PreflightAFK(sha="abc123", issue_number=99)
            ),
        ),
        cfg=Config(auto_push=False),
    )
    issues = [{"number": 1, "title": "Clean"}, {"number": 2, "title": "Conflict"}]
    _run(issues, local_deps)
    git_svc.push.assert_not_called()


def test_auto_push_does_not_push_when_no_issues_processed(deps, git_svc):
    _run([], deps)
    git_svc.push.assert_not_called()


def test_push_git_command_error_propagates(deps, git_svc):
    git_svc.push.side_effect = GitCommandError("push failed", returncode=1, stderr="")
    issues = [{"number": 1, "title": "Fix A"}]
    with pytest.raises(GitCommandError):
        _run(issues, deps)


# ── Merger session cleanup after successful conflict resolution ───────────────


def test_merge_phase_removes_merger_session_dir_after_successful_conflict_resolution(
    tmp_path, git_svc, github_svc
):
    """merge_phase removes the merger session dir entirely on success.

    Merge-sandbox has no downstream stage that needs a stage-done sentinel, so the
    dir must be gone before managed_worktree's teardown predicate runs — otherwise
    `any_role_dir_present` would preserve the sandbox and it would leak.
    Intercepts remove_worktree to assert the dir is absent at that point.
    """
    git_svc.try_merge.return_value = False

    sandbox_path = tmp_path / Config().pycastle_dir / ".worktrees" / "merge-sandbox"
    orig_create = git_svc.create_worktree.side_effect
    orig_remove = git_svc.remove_worktree.side_effect

    captured: dict = {}

    def _create_with_session(repo, wt, branch, sha=None):
        orig_create(repo, wt, branch, sha)
        if wt == sandbox_path:
            session_dir = wt / ".pycastle-session" / "merger"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "session.json").write_text("{}")

    def _capture_on_remove(repo, wt):
        if wt == sandbox_path:
            merger_dir = sandbox_path / ".pycastle-session" / "merger"
            captured["exists"] = merger_dir.is_dir()
            captured["empty"] = (
                not any(merger_dir.iterdir()) if merger_dir.is_dir() else None
            )
        if orig_remove:
            orig_remove(repo, wt)

    git_svc.create_worktree.side_effect = _create_with_session
    git_svc.remove_worktree.side_effect = _capture_on_remove

    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)

    assert captured.get("exists") is False, (
        "merger session dir should be removed before teardown"
    )


def test_merge_phase_tears_down_sandbox_after_merger_session_cleanup(
    tmp_path, git_svc, github_svc
):
    """After merger session cleanup, branch_worktree must tear down the sandbox normally."""
    git_svc.try_merge.return_value = False

    sandbox_path = tmp_path / Config().pycastle_dir / ".worktrees" / "merge-sandbox"
    orig_create = git_svc.create_worktree.side_effect

    def _create_with_session(repo, wt, branch, sha=None):
        orig_create(repo, wt, branch, sha)
        if wt == sandbox_path:
            session_dir = wt / ".pycastle-session" / "merger"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "session.json").write_text("{}")

    git_svc.create_worktree.side_effect = _create_with_session

    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)

    git_svc.remove_worktree.assert_called_once_with(deps.repo_root, sandbox_path)


# ── Merger session resume parity ──────────────────────────────────────────────


def test_merge_phase_preserves_sandbox_and_session_on_usage_limit_error(
    tmp_path, git_svc, github_svc
):
    """UsageLimitError during merger leaves sandbox worktree and session dir on disk."""
    from pycastle.errors import UsageLimitError

    git_svc.try_merge.return_value = False
    sandbox_path = tmp_path / Config().pycastle_dir / ".worktrees" / "merge-sandbox"

    def _raise_after_seed(request):
        session_dir = sandbox_path / ".pycastle-session" / "merger"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "session.json").write_text("{}")
        raise UsageLimitError()

    fake = FakeAgentRunner(side_effect=_raise_after_seed)
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    issues = [{"number": 1, "title": "Conflict"}]

    with pytest.raises(UsageLimitError):
        _run(issues, deps)

    assert sandbox_path.exists(), "sandbox worktree must be preserved"
    session_dir = sandbox_path / ".pycastle-session" / "merger"
    assert session_dir.exists() and any(session_dir.rglob("*"))


def test_merge_phase_tears_down_and_deletes_branch_when_clean_sandbox_and_no_session(
    tmp_path, git_svc, github_svc
):
    """With clean sandbox and no session, both teardown_worktree and delete_branch fire."""
    git_svc.try_merge.return_value = False
    sandbox_path = tmp_path / Config().pycastle_dir / ".worktrees" / "merge-sandbox"

    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)

    git_svc.remove_worktree.assert_called_with(deps.repo_root, sandbox_path)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/merge-sandbox" in deleted


def test_merge_phase_reuses_existing_sandbox_when_merger_session_is_resumable(
    tmp_path, git_svc, github_svc
):
    """When sandbox exists with a resumable session, branch_worktree must not re-create it."""
    git_svc.try_merge.return_value = False
    sandbox_path = tmp_path / Config().pycastle_dir / ".worktrees" / "merge-sandbox"

    sandbox_path.mkdir(parents=True)
    (sandbox_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    session_dir = sandbox_path / ".pycastle-session" / "merger"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}")
    git_svc.get_current_branch.return_value = "pycastle/merge-sandbox"

    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)

    create_calls = [
        call
        for call in git_svc.create_worktree.call_args_list
        if call.args[1] == sandbox_path
    ]
    assert not create_calls, (
        "create_worktree must not be called for an existing resumable sandbox"
    )


# ── Parallel branch teardown: warning routing ─────────────────────────────────


def test_worktree_removal_warning_routed_to_status_display_not_stderr(
    recording_deps, git_svc, capsys
):
    """When worktree removal fails, warning must go to status_display.print, not stderr."""
    deps, recording = recording_deps
    wt_path = deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / "issue-1"
    git_svc.list_worktrees.return_value = [wt_path]
    git_svc.remove_worktree.side_effect = RuntimeError("disk full")
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    print_msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("could not remove worktree" in str(m) for m in print_msgs)
    assert "could not remove worktree" not in capsys.readouterr().err


def test_branch_deletion_warning_routed_to_status_display_not_stderr(
    recording_deps, git_svc, capsys
):
    """When delete_branch fails, warning must go to status_display.print, not stderr."""
    deps, recording = recording_deps
    git_svc.delete_branch.side_effect = GitCommandError("fail", returncode=1, stderr="")
    issues = [{"number": 1, "title": "Fix A"}]
    _run(issues, deps)

    print_msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("could not delete branch" in str(m) for m in print_msgs)
    assert "could not delete branch" not in capsys.readouterr().err


def test_all_branches_processed_in_parallel_teardown(recording_deps, git_svc):
    """All branches must be deleted even when processed in parallel."""
    deps, recording = recording_deps
    issues = [
        {"number": 1, "title": "A"},
        {"number": 2, "title": "B"},
        {"number": 3, "title": "C"},
    ]
    _run(issues, deps)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-1" in deleted
    assert "pycastle/issue-2" in deleted
    assert "pycastle/issue-3" in deleted


# ── Parallel close_issue helper ───────────────────────────────────────────────


def test_close_issue_failure_does_not_abort_merge_phase(
    recording_deps, github_svc
):
    deps, recording = recording_deps
    github_svc.close_issue.side_effect = RuntimeError("API error")
    issues = [{"number": 1, "title": "Fix A"}]
    result = _run(issues, deps)
    assert result.clean == issues
    print_msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("API error" in str(m) for m in print_msgs)


def test_close_issue_failure_in_conflict_path_does_not_abort(
    recording_deps, git_svc, github_svc
):
    deps, recording = recording_deps
    git_svc.try_merge.return_value = False
    github_svc.close_issue.side_effect = RuntimeError("conflict close failed")
    issues = [{"number": 1, "title": "Conflict"}]
    result = _run(issues, deps)
    assert result.conflicts == issues
    print_msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("conflict close failed" in str(m) for m in print_msgs)


def test_close_issue_all_failures_reported_via_status_display(
    recording_deps, github_svc
):
    deps, recording = recording_deps
    errors = {1: RuntimeError("first"), 2: RuntimeError("second")}

    def _side_effect(number):
        raise errors[number]

    github_svc.close_issue.side_effect = _side_effect
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    _run(issues, deps)
    print_msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("first" in str(m) for m in print_msgs)
    assert any("second" in str(m) for m in print_msgs)


def test_merge_phase_shows_closing_progress_during_clean_merge(recording_deps):
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    _run(issues, deps)
    update_calls = [
        c for c in recording.calls if c[0] == "update_phase" and c[1] == "Merge"
    ]
    closing_calls = [c for c in update_calls if "Closing" in c[2]]
    assert closing_calls, "update_phase must be called with 'Closing X/N issues' text"


def test_merge_phase_shows_closing_progress_during_conflict_path(
    recording_deps, git_svc
):
    deps, recording = recording_deps
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)
    update_calls = [
        c for c in recording.calls if c[0] == "update_phase" and c[1] == "Merge"
    ]
    closing_calls = [c for c in update_calls if "Closing" in c[2]]
    assert closing_calls, "update_phase must be called with 'Closing X/N issues' text"


def test_merge_phase_progress_counter_reaches_total(recording_deps):
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    _run(issues, deps)
    update_calls = [
        c for c in recording.calls if c[0] == "update_phase" and c[1] == "Merge"
    ]
    closing_texts = [c[2] for c in update_calls if "Closing" in c[2]]
    assert "Closing 2/2 issues" in closing_texts


# ── Teardown on_progress and input-order preservation ─────────────────────────


def test_teardown_progress_fires_for_every_branch_including_non_ancestor_skips(
    recording_deps, git_svc
):
    """on_progress fires for each branch including non-ancestors that are skipped."""
    deps, recording = recording_deps

    def _is_ancestor(branch, repo_root):
        return "issue-1" in branch  # issue-2 is NOT an ancestor

    git_svc.is_ancestor.side_effect = _is_ancestor
    issues = [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}]
    _run(issues, deps)

    update_calls = [
        c for c in recording.calls if c[0] == "update_phase" and c[1] == "Merge"
    ]
    removing_texts = [c[2] for c in update_calls if "removing" in c[2]]
    assert any("removing 2/2" in t for t in removing_texts), (
        "Progress must reach 2/2 including the non-ancestor skip"
    )


def test_teardown_progress_fires_for_tolerated_delete_failures(recording_deps, git_svc):
    """on_progress fires even when delete_branch raises a tolerated GitCommandError."""
    deps, recording = recording_deps

    def _delete_with_failure(branch, repo_root):
        if "issue-1" in branch:
            raise GitCommandError("fail", returncode=1, stderr="")

    git_svc.delete_branch.side_effect = _delete_with_failure
    issues = [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}]
    _run(issues, deps)

    update_calls = [
        c for c in recording.calls if c[0] == "update_phase" and c[1] == "Merge"
    ]
    removing_texts = [c[2] for c in update_calls if "removing" in c[2]]
    assert any("removing 2/2" in t for t in removing_texts), (
        "Progress must reach 2/2 even when one delete fails"
    )


def test_deleted_branches_preserve_input_order(recording_deps, git_svc):
    """The close message lists deleted branches in the same order as the input."""
    deps, recording = recording_deps
    issues = [
        {"number": 3, "title": "C"},
        {"number": 1, "title": "A"},
        {"number": 2, "title": "B"},
    ]
    _run(issues, deps)

    remove_calls = [c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"]
    msg = remove_calls[-1][2]
    assert msg.index("issue-3") < msg.index("issue-1") < msg.index("issue-2")


def test_deleted_branches_preserve_input_order_under_staggered_completion(
    recording_deps, git_svc
):
    """Input order is preserved even when the first branch's delete completes last."""
    import threading

    deps, recording = recording_deps
    event = threading.Event()

    def _staggered_delete(branch, repo_root):
        if "issue-3" in branch:
            event.wait(timeout=2)  # wait until issue-2 has completed
        else:
            event.set()

    git_svc.delete_branch.side_effect = _staggered_delete
    issues = [{"number": 3, "title": "C"}, {"number": 2, "title": "B"}]
    _run(issues, deps)

    remove_calls = [c for c in recording.calls if c[0] == "remove" and c[1] == "Merge"]
    msg = remove_calls[-1][2]
    assert msg.index("issue-3") < msg.index("issue-2"), (
        "issue-3 (first in input) must appear before issue-2 even though issue-2 completed first"
    )


def test_uncaught_exception_in_teardown_does_not_cancel_siblings(
    recording_deps, git_svc
):
    """An uncaught exception from is_ancestor in one task does not abort sibling teardowns."""
    deps, recording = recording_deps

    def _is_ancestor_raises_for_one(branch, repo_root):
        if "issue-1" in branch:
            raise RuntimeError("is_ancestor failed")
        return True

    git_svc.is_ancestor.side_effect = _is_ancestor_raises_for_one
    issues = [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}]
    _run(issues, deps)

    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-2" in deleted, "Sibling issue-2 must still be processed"


def test_uncaught_exception_in_teardown_forwarded_to_status_display(
    recording_deps, git_svc
):
    """An uncaught exception escaping _teardown_one is forwarded to status_display.print as a warning."""
    deps, recording = recording_deps

    def _is_ancestor_raises(branch, repo_root):
        raise RuntimeError("unexpected teardown failure")

    git_svc.is_ancestor.side_effect = _is_ancestor_raises
    issues = [{"number": 1, "title": "A"}]
    _run(issues, deps)

    print_msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("unexpected teardown failure" in str(m) for m in print_msgs)


def test_phase_row_shows_removing_progress_during_clean_teardown(recording_deps):
    """During clean branch teardown, phase row reads 'Closing X/N issues, removing Y/M worktrees'."""
    deps, recording = recording_deps
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]
    _run(issues, deps)

    update_calls = [
        c for c in recording.calls if c[0] == "update_phase" and c[1] == "Merge"
    ]
    removing_texts = [c[2] for c in update_calls if "removing" in c[2]]
    assert removing_texts, (
        "Phase row must show 'removing Y/M worktrees' during teardown"
    )
    assert all(
        "Closing" in t and "issues" in t and "removing" in t for t in removing_texts
    )


def test_phase_row_shows_removing_progress_during_conflict_teardown(
    recording_deps, git_svc
):
    """During conflict branch teardown, phase row reads 'Closing X/N issues, removing Y/M worktrees'."""
    deps, recording = recording_deps
    git_svc.try_merge.return_value = False
    issues = [{"number": 1, "title": "Conflict"}]
    _run(issues, deps)

    update_calls = [
        c for c in recording.calls if c[0] == "update_phase" and c[1] == "Merge"
    ]
    removing_texts = [c[2] for c in update_calls if "removing" in c[2]]
    assert removing_texts, (
        "Phase row must show 'removing Y/M worktrees' during conflict teardown"
    )
    assert all("Closing" in t and "removing" in t for t in removing_texts)


# ── Close-failure resilience ──────────────────────────────────────────────────


def _api_error(status: int) -> GithubAPIError:
    return GithubAPIError("fail", status=status, body="err", method="PATCH", path="/x")


def test_merge_phase_proceeds_to_branch_deletion_when_all_closes_fail(
    recording_deps, git_svc, github_svc
):
    """Branch deletion runs even when every close_issue raises."""
    deps, _ = recording_deps
    github_svc.close_issue.side_effect = _api_error(500)
    issues = [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}]
    _run(issues, deps)
    deleted = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-1" in deleted
    assert "pycastle/issue-2" in deleted


def test_merge_phase_proceeds_to_parent_close_when_all_closes_fail(
    recording_deps, github_svc
):
    """close_completed_parent_issues runs even when every close_issue raises."""
    deps, _ = recording_deps
    github_svc.close_issue.side_effect = _api_error(500)
    issues = [{"number": 1, "title": "A"}]
    _run(issues, deps)
    assert github_svc.close_completed_parent_issues.call_count == 1


def test_merge_phase_surfaces_close_failure_via_status_display(
    recording_deps, github_svc
):
    """A close_issue failure is printed to status_display with the issue number and message."""
    deps, recording = recording_deps
    github_svc.close_issue.side_effect = _api_error(500)
    issues = [{"number": 42, "title": "A"}]
    _run(issues, deps)
    print_msgs = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("42" in str(m) for m in print_msgs)


def test_merge_phase_partial_close_failure_still_closes_successful_issue(
    recording_deps, github_svc
):
    """When one close fails (500) and one succeeds, the successful close is recorded."""
    deps, _ = recording_deps

    def _side_effect(number: int) -> None:
        if number == 1:
            raise _api_error(500)

    github_svc.close_issue.side_effect = _side_effect
    issues = [{"number": 1, "title": "Fail"}, {"number": 2, "title": "OK"}]
    result = _run(issues, deps)
    assert result.clean == issues
    closed = [call.args[0] for call in github_svc.close_issue.call_args_list]
    assert 2 in closed
