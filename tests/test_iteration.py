import asyncio
import dataclasses
from unittest.mock import MagicMock

import pytest

from pycastle.errors import UsageLimitError
from pycastle.config import Config
from pycastle.services import GitService
from pycastle.services import GithubService
from pycastle.iteration import (
    AbortedHITL,
    AbortedUsageLimit,
    Continue,
    Done,
    run_iteration,
)
from pycastle.agent_runner import RunRequest
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
)
from pycastle.status_display import PlainStatusDisplay
from pycastle.agent_output_protocol import (
    CompletionOutput,
    IssueOutput,
    PlannerOutput,
    PromiseParseError,
)


def _plan_output(issues: list[dict]) -> PlannerOutput:
    return PlannerOutput(
        issues=[{"number": i["number"], "title": i["title"]} for i in issues]
    )


@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    svc.try_merge.return_value = True
    svc.is_ancestor.return_value = True
    svc.verify_ref_exists.return_value = False
    return svc


@pytest.fixture
def github_svc():
    svc = MagicMock(spec=GithubService)
    svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    return svc


@pytest.fixture
def logger():
    return RecordingLogger()


def _make_deps(
    tmp_path,
    run_agent_fn,
    *,
    git_svc,
    github_svc,
    logger,
    cfg=None,
    status_display=None,
    preflight_responses=None,
) -> Deps:
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=FakeAgentRunner(
            side_effect=run_agent_fn,
            preflight_responses=[[]]
            if preflight_responses is None
            else preflight_responses,
        ),
        cfg=cfg or Config(max_parallel=4, max_iterations=1),
        logger=logger,
        status_display=status_display or PlainStatusDisplay(),  # type: ignore[arg-type]
    )


# ── Done: no open issues ──────────────────────────────────────────────────────


def test_run_iteration_returns_done_when_no_open_issues(tmp_path, git_svc, logger):
    """run_iteration returns Done when plan_phase finds no open issues."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    async def _noop_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _noop_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Done)


# ── AbortedHITL: HITL preflight verdict ──────────────────────────────────────


def test_run_iteration_returns_aborted_hitl_on_hitl_verdict(tmp_path, git_svc, logger):
    """run_iteration returns AbortedHITL when preflight_phase returns PreflightHITL."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=42, labels=["ready-for-human"])

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[(("ruff", "ruff check .", "E501"),)],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHITL)
    assert result.issue_number == 42


def test_run_iteration_aborted_hitl_carries_issue_number(tmp_path, git_svc, logger):
    """AbortedHITL must carry the issue number filed by the preflight-issue agent."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=99, labels=["ready-for-human"])

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[(("mypy", "mypy .", "error: Missing module"),)],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHITL)
    assert result.issue_number == 99


def test_run_iteration_aborted_hitl_does_not_raise_system_exit(
    tmp_path, git_svc, logger
):
    """run_iteration must return AbortedHITL instead of calling sys.exit on HITL verdict."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=7, labels=["ready-for-human"])

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[(("ruff", "ruff check .", "E501"),)],
    )

    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, AbortedHITL)


# ── AbortedUsageLimit: usage limit hit ───────────────────────────────────────


def test_run_iteration_raises_when_planner_hits_usage_limit(tmp_path, git_svc, logger):
    """run_iteration propagates UsageLimitError when the Planner raises it."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix A"},
        {"number": 2, "title": "Fix B"},
    ]

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError("token ceiling reached")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[]],
    )
    with pytest.raises(UsageLimitError):
        asyncio.run(run_iteration(deps))


def test_run_iteration_returns_aborted_usage_limit_when_implementer_hits_limit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns AbortedUsageLimit when an implementer hits the usage limit."""

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError("")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)


def test_run_iteration_aborted_usage_limit_does_not_raise_system_exit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must return AbortedUsageLimit instead of calling sys.exit on usage limit."""

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError("")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, AbortedUsageLimit)


# ── Continue: normal iteration completion ─────────────────────────────────────


def test_run_iteration_returns_continue_when_issues_complete_normally(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns Continue after a normal implement→merge cycle (1-issue fast path)."""

    async def _fake_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


def test_run_iteration_returns_continue_when_no_implementers_complete(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns Continue (not Done) when implementers produce no commits."""

    async def _fake_agent(request: RunRequest):
        raise PromiseParseError("no <promise>COMPLETE</promise> tag")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


# ── PreflightAFK: preflight failure with AFK verdict ─────────────────────────


def test_run_iteration_returns_continue_on_afk_preflight_verdict(
    tmp_path, git_svc, logger
):
    """run_iteration implements the preflight-fix issue and returns Continue when
    preflight_phase returns PreflightAFK (preflight failure with AFK verdict)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Preflight fix"

    async def _fake_agent(request: RunRequest):
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=55, labels=["ready-for-agent"])
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[(("ruff", "ruff check .", "E501"),)],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


def test_run_iteration_afk_path_spawns_implementer_for_fix_issue(
    tmp_path, git_svc, logger
):
    """On AFK preflight verdict, run_iteration must spawn an Implementer for the
    filed fix issue without invoking the Planner."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Preflight fix"

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=77, labels=["ready-for-agent"])
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[(("mypy", "mypy .", "error"),)],
    )
    asyncio.run(run_iteration(deps))

    implementer_calls = [n for n in agent_names if "Implement Agent" in n]
    assert "Plan Agent" not in agent_names, (
        "Plan Agent must not be called on AFK preflight path"
    )
    assert len(implementer_calls) == 1, "Exactly one Implement Agent for the fix issue"
    assert implementer_calls[0] == "Implement Agent #77"


# ── StatusDisplay routing ──────────────────────────────────────────────────────


def test_run_iteration_routes_planning_complete_through_status_display(
    tmp_path, git_svc, github_svc, logger, capsys
):
    """run_iteration must route the planning-complete summary through status_display.print()."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("Planning complete" in msg for msg in print_messages)
    assert "Planning complete" not in capsys.readouterr().out


def test_run_iteration_execution_complete_uses_consistent_source(
    tmp_path, git_svc, github_svc, logger
):
    """The execution-complete block must use a single consistent source for automatic blank-line separation."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    exec_prints = [
        (caller, msg)
        for c in recording.calls
        if c[0] == "print"
        for caller, msg in [(c[1], c[2])]
        if "Execution complete" in str(msg) or str(msg).startswith("  pycastle/")
    ]
    assert exec_prints, "Expected execution-complete messages"
    callers = {caller for caller, _ in exec_prints}
    assert len(callers) == 1, (
        f"Expected all execution-complete messages to share one caller, got: {callers}"
    )


def test_run_iteration_routes_hitl_abort_message_through_status_display(
    tmp_path, git_svc, logger, capsys
):
    """run_iteration must route the HITL abort message through status_display.print()."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=42, labels=["ready-for-human"])

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
        preflight_responses=[(("ruff", "ruff check .", "E501"),)],
    )
    asyncio.run(run_iteration(deps))

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("human intervention" in msg for msg in print_messages)
    assert "human intervention" not in capsys.readouterr().out


def test_run_iteration_routes_no_commits_message_through_status_display(
    tmp_path, git_svc, github_svc, logger, capsys
):
    """run_iteration must route 'No commits produced' through status_display.print()."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output([{"number": 1, "title": "Fix bug"}])
        raise PromiseParseError("no <promise>COMPLETE</promise> tag")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("No commits" in msg for msg in print_messages)
    assert "No commits" not in capsys.readouterr().out


# ── One-issue fast path ───────────────────────────────────────────────────────


def test_run_iteration_calls_planning_phase_with_two_or_more_open_issues(
    tmp_path, git_svc, logger
):
    """With two or more open issues and passing preflight, run_iteration must invoke
    the Planner (planning_phase) before implement_phase."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 3, "title": "Issue A"},
        {"number": 7, "title": "Issue B"},
    ]

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        if request.name == "Plan Agent":
            return _plan_output([{"number": 3, "title": "Issue A"}])
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[]],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert "Plan Agent" in agent_names, (
        "Plan Agent must be called when two or more issues exist"
    )


def test_run_iteration_skips_planning_phase_with_one_open_issue(
    tmp_path, git_svc, logger
):
    """With exactly one open issue and passing preflight, run_iteration must not
    invoke the Planner and must pass the issue directly to implement_phase."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 7, "title": "Single issue"}]

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[]],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert "Plan Agent" not in agent_names, (
        "Plan Agent must not be called for a single issue"
    )
    assert any("Implement Agent" in n for n in agent_names), (
        "Implement Agent must be called"
    )


def test_run_iteration_returns_continue_when_planning_phase_selects_no_issues(
    tmp_path, git_svc, logger
):
    """When planning_phase returns zero issues (Planner picks none), run_iteration
    produces no commits and returns Continue."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A"},
        {"number": 2, "title": "Issue B"},
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output([])
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[]],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


# ── work_body ─────────────────────────────────────────────────────────────────


def test_implementer_and_reviewer_run_calls_pass_work_body_with_issue_title(
    tmp_path, git_svc, github_svc, logger
):
    issue_title = "Fix auth bug"
    github_svc.get_open_issues.return_value = [{"number": 3, "title": issue_title}]
    recording_runner = FakeAgentRunner(
        [
            _plan_output([{"number": 3, "title": issue_title}]),
            CompletionOutput(),
            CompletionOutput(),
        ],
        preflight_responses=[[]],
    )
    deps = dataclasses.replace(
        _make_deps(
            tmp_path, None, git_svc=git_svc, github_svc=github_svc, logger=logger
        ),
        agent_runner=recording_runner,
    )

    asyncio.run(run_iteration(deps))

    implementer_calls = [
        c for c in recording_runner.calls if "Implement Agent" in c.name
    ]
    reviewer_calls = [c for c in recording_runner.calls if "Review Agent" in c.name]
    assert len(implementer_calls) == 1
    assert implementer_calls[0].work_body == f'implementing "{issue_title}"'
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0].work_body == f'reviewing "{issue_title}"'


def test_planner_run_call_passes_work_body_with_issue_count(
    tmp_path, git_svc, github_svc, logger
):
    open_issues = [
        {"number": 1, "title": "Fix A"},
        {"number": 2, "title": "Fix B"},
        {"number": 3, "title": "Fix C"},
    ]
    github_svc.get_open_issues.return_value = open_issues
    recording_runner = FakeAgentRunner(
        [
            _plan_output([{"number": 1, "title": "Fix A"}]),
            CompletionOutput(),
            CompletionOutput(),
        ],
        preflight_responses=[[]],
    )
    deps = dataclasses.replace(
        _make_deps(
            tmp_path, None, git_svc=git_svc, github_svc=github_svc, logger=logger
        ),
        agent_runner=recording_runner,
    )

    asyncio.run(run_iteration(deps))

    planner_calls = [c for c in recording_runner.calls if c.name == "Plan Agent"]
    assert len(planner_calls) == 1
    assert planner_calls[0].work_body == f"Creating Plan from {len(open_issues)} issues"


# ── Display row lifecycle ──────────────────────────────────────────────────────


def test_run_iteration_preflight_row_removed_even_if_preflight_raises(tmp_path, logger):
    """run_iteration must remove the 'Preflight' display row even when preflight_phase raises."""
    from pycastle.services import GitCommandError

    recording = RecordingStatusDisplay()
    git_svc = MagicMock(spec=GitService)
    git_svc.is_working_tree_clean.return_value = True
    git_svc.pull.side_effect = GitCommandError("pull failed", returncode=1, stderr="")
    git_svc.get_head_sha.return_value = "abc123"

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]

    deps = _make_deps(
        tmp_path,
        None,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )

    with pytest.raises(GitCommandError):
        asyncio.run(run_iteration(deps))

    assert ("remove", "Preflight", "finished", "success") in recording.calls


def test_run_iteration_plan_row_removed_even_if_planning_raises(
    tmp_path, git_svc, logger
):
    """run_iteration must remove the 'Plan' display row even when planning_phase raises."""
    recording = RecordingStatusDisplay()

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A"},
        {"number": 2, "title": "Issue B"},
    ]

    async def _bad_planner(request: RunRequest):
        raise RuntimeError("planner exploded")

    deps = _make_deps(
        tmp_path,
        _bad_planner,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )

    with pytest.raises(RuntimeError, match="planner exploded"):
        asyncio.run(run_iteration(deps))

    assert ("remove", "Plan", "finished", "success") in recording.calls


def test_run_iteration_implement_row_removed_on_usage_limit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must remove the 'Implement' display row even when usage limit is hit."""
    recording = RecordingStatusDisplay()

    async def _usage_limit(request: RunRequest):
        raise UsageLimitError("")

    deps = _make_deps(
        tmp_path,
        _usage_limit,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert ("remove", "Implement", "finished", "success") in recording.calls


def test_run_iteration_registers_preflight_row_before_preflight_phase(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must register the 'Preflight' row before calling preflight_phase."""
    recording = RecordingStatusDisplay()

    async def _noop_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _noop_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    register_idx = next(
        (
            i
            for i, c in enumerate(recording.calls)
            if c[:2] == ("register", "Preflight")
        ),
        None,
    )
    remove_idx = next(
        (i for i, c in enumerate(recording.calls) if c[:2] == ("remove", "Preflight")),
        None,
    )
    assert register_idx is not None, "Preflight row must be registered"
    assert remove_idx is not None, "Preflight row must be removed"
    assert register_idx < remove_idx


# ── Planning skip when in-flight branches or worktrees exist ─────────────────


def test_run_iteration_skips_planning_when_all_issues_have_existing_branches(
    tmp_path, git_svc, logger
):
    """When all open issues have an existing branch, planning_phase is not invoked
    and the iteration proceeds with those issues as the working set."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix A"},
        {"number": 2, "title": "Fix B"},
    ]
    git_svc.verify_ref_exists.return_value = True

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert "Plan Agent" not in agent_names, (
        "Plan Agent must not be called when all branches exist"
    )
    assert any("Implement Agent" in n for n in agent_names), (
        "Implement Agent must still run"
    )


def test_run_iteration_skips_planning_when_all_issues_have_existing_worktrees(
    tmp_path, git_svc, logger
):
    """When all open issues have an existing worktree directory (but no branch),
    planning_phase is not invoked."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 3, "title": "Fix C"},
        {"number": 4, "title": "Fix D"},
    ]
    git_svc.verify_ref_exists.return_value = False
    for n in [3, 4]:
        (tmp_path / "pycastle" / ".worktrees" / f"issue-{n}").mkdir(parents=True)

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert "Plan Agent" not in agent_names, (
        "Plan Agent must not be called when all worktrees exist"
    )


def test_run_iteration_uses_only_in_flight_issues_when_some_have_existing_branch(
    tmp_path, git_svc, logger
):
    """When only some open issues have an existing branch, only those in-flight issues
    are used as the working set and planning_phase is not invoked."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 5, "title": "In flight"},
        {"number": 6, "title": "Deferred"},
    ]
    git_svc.verify_ref_exists.side_effect = lambda ref, path: ref == "pycastle/issue-5"

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert "Plan Agent" not in agent_names, (
        "Plan Agent must not be called when some branches exist"
    )
    assert "Implement Agent #5" in agent_names, "In-flight issue must be implemented"
    assert not any("Implement Agent #6" in n for n in agent_names), (
        "Deferred issue must not be implemented"
    )


def test_run_iteration_uses_preflight_sha_for_in_flight_issues(
    tmp_path, git_svc, logger
):
    """When in-flight issues are used, the implement phase receives the preflight SHA
    unchanged — the in-flight path must not re-pin the SHA from a plan-sandbox."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 7, "title": "In flight"}]
    git_svc.verify_ref_exists.return_value = True
    git_svc.get_head_sha.return_value = "preflight-sha-abc"

    async def _fake_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(run_iteration(deps))

    implement_shas = {
        c.args[3]
        for c in git_svc.create_worktree.call_args_list
        if c.args[3] is not None
    }
    assert "preflight-sha-abc" in implement_shas, (
        "Implement phase must use the preflight SHA, not a re-pinned SHA"
    )


def test_run_iteration_detects_in_flight_via_both_branch_and_worktree_signals(
    tmp_path, git_svc, logger
):
    """Both detection signals (branch and worktree) are checked independently:
    an issue with only a branch, an issue with only a worktree directory, and
    an issue with neither are handled correctly in a single iteration."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 8, "title": "Branch only"},
        {"number": 9, "title": "Worktree only"},
        {"number": 10, "title": "Deferred"},
    ]
    git_svc.verify_ref_exists.side_effect = lambda ref, path: ref == "pycastle/issue-8"
    (tmp_path / "pycastle" / ".worktrees" / "issue-9").mkdir(parents=True)

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert "Plan Agent" not in agent_names
    assert "Implement Agent #8" in agent_names, "Branch-only in-flight issue must run"
    assert "Implement Agent #9" in agent_names, "Worktree-only in-flight issue must run"
    assert not any("Implement Agent #10" in n for n in agent_names), (
        "Deferred issue must not run"
    )
