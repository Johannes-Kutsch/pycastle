import asyncio
import dataclasses
from unittest.mock import MagicMock

import pytest

from pycastle.errors import UsageLimitError
from pycastle.config import Config
from pycastle.services import GitService
from pycastle.services import GithubService
from pycastle.iteration import (
    AbortedAgentFailure,
    AbortedHITL,
    AbortedUsageLimit,
    Continue,
    Done,
    NoCandidate,
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
    AgentRole,
    CompletionOutput,
    FailedOutput,
    IssueOutput,
    NoCandidateOutput,
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
    svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]
    svc.get_all_open_issues_lightweight.return_value = []
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
    import shutil as _shutil

    _cfg = cfg or Config(max_parallel=4, max_iterations=1)
    _registered: list = []

    def _fake_list_worktrees(repo):
        return list(_registered)

    def _fake_create_worktree(repo, path, branch, sha=None):
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n")
        _registered.append(path)

    def _fake_remove_worktree(repo, path):
        _shutil.rmtree(path, ignore_errors=True)
        _registered[:] = [p for p in _registered if p != path]

    git_svc.list_worktrees.side_effect = _fake_list_worktrees
    git_svc.create_worktree.side_effect = _fake_create_worktree
    git_svc.remove_worktree.side_effect = _fake_remove_worktree
    if isinstance(github_svc.get_all_open_issues_lightweight.return_value, MagicMock):
        github_svc.get_all_open_issues_lightweight.return_value = []

    return Deps(
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=FakeAgentRunner(
            side_effect=run_agent_fn,
            preflight_responses=[[]]
            if preflight_responses is None
            else preflight_responses,
        ),
        cfg=_cfg,
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
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]

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
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]

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
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]

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


def test_run_iteration_returns_aborted_usage_limit_when_planner_hits_limit(
    tmp_path, git_svc, logger
):
    """run_iteration returns AbortedUsageLimit when the Planner hits the usage limit,
    so the orchestrator can fail over to a standby account instead of crashing."""
    from datetime import datetime

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix A", "body": "", "comments": []},
        {"number": 2, "title": "Fix B", "body": "", "comments": []},
    ]
    reset_time = datetime(2026, 5, 7, 13, 10)

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError(reset_time=reset_time)

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[]],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert result.reset_time == reset_time


def test_run_iteration_returns_aborted_usage_limit_when_implementer_hits_limit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns AbortedUsageLimit when an implementer hits the usage limit."""

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
        raise UsageLimitError(reset_time=None)

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
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
        raise UsageLimitError(reset_time=None)

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, AbortedUsageLimit)


# ── Continue: normal iteration completion ─────────────────────────────────────


def test_run_iteration_returns_continue_when_issues_complete_normally(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns Continue after a normal plan→implement→merge cycle."""

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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
    """run_iteration plans and implements the preflight-fix issue and returns Continue when
    preflight_phase returns PreflightAFK (preflight failure with AFK verdict)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]
    github_svc.get_issue_title.return_value = "Preflight fix"

    async def _fake_agent(request: RunRequest):
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=55, labels=["ready-for-agent"])
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 55, "title": "Preflight fix", "body": "", "comments": []}]
            )
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


def test_run_iteration_afk_path_routes_through_planning_then_implements_fix_issue(
    tmp_path, git_svc, logger
):
    """On AFK preflight verdict, run_iteration must invoke the Planner and then
    spawn an Implementer for the filed fix issue."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]
    github_svc.get_issue_title.return_value = "Preflight fix"

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=77, labels=["ready-for-agent"])
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 77, "title": "Preflight fix", "body": "", "comments": []}]
            )
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
    assert "Plan Agent" in agent_names, (
        "Plan Agent must be called on AFK preflight path"
    )
    assert len(implementer_calls) == 1, "Exactly one Implement Agent for the fix issue"
    assert implementer_calls[0] == "Implement Agent #77"


# ── StatusDisplay routing ──────────────────────────────────────────────────────


def test_run_iteration_routes_planning_complete_through_status_display(
    tmp_path, git_svc, logger, capsys
):
    """run_iteration must route the planning-complete summary through status_display (as the Plan row close message)."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A", "body": "", "comments": []},
        {"number": 2, "title": "Issue B", "body": "", "comments": []},
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Issue A", "body": "", "comments": []}]
            )
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

    remove_messages = [
        c[2] for c in recording.calls if c[0] == "remove" and c[1] == "Plan"
    ]
    assert any("Planning complete" in msg for msg in remove_messages)
    assert "Planning complete" not in capsys.readouterr().out


def test_run_iteration_execution_complete_uses_consistent_source(
    tmp_path, git_svc, github_svc, logger
):
    """The execution-complete summary is emitted as the Implement row close message with caller 'Implement'."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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

    impl_removes = [
        c
        for c in recording.calls
        if c[0] == "remove"
        and c[1] == "Implement"
        and "Execution complete" in str(c[2])
    ]
    assert impl_removes, (
        "Expected Implement row removed with 'Execution complete' message"
    )
    msg = impl_removes[0][2]
    assert "pycastle/issue-" in msg, "Branch name must appear in the close message"


def test_run_iteration_routes_hitl_abort_message_through_status_display(
    tmp_path, git_svc, logger, capsys
):
    """run_iteration must route the HITL abort message through status_display.print()."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]

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
    """run_iteration must route 'No commits produced' through status_display (as the Implement row close message)."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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

    remove_messages = [
        c[2] for c in recording.calls if c[0] == "remove" and c[1] == "Implement"
    ]
    assert any("No commits" in msg for msg in remove_messages)
    assert "No commits" not in capsys.readouterr().out


# ── One-issue fast path ───────────────────────────────────────────────────────


def test_run_iteration_calls_planning_phase_with_two_or_more_open_issues(
    tmp_path, git_svc, logger
):
    """With two or more open issues and passing preflight, run_iteration must invoke
    the Planner (planning_phase) before implement_phase."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 3, "title": "Issue A", "body": "", "comments": []},
        {"number": 7, "title": "Issue B", "body": "", "comments": []},
    ]

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 3, "title": "Issue A", "body": "", "comments": []}]
            )
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


def test_run_iteration_calls_planning_phase_with_one_open_issue(
    tmp_path, git_svc, logger
):
    """With exactly one open issue and passing preflight, run_iteration must invoke
    the Planner (planning_phase) before implement_phase."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 7, "title": "Single issue", "body": "", "comments": []}
    ]

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 7, "title": "Single issue", "body": "", "comments": []}]
            )
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
    assert "Plan Agent" in agent_names, "Plan Agent must be called for a single issue"
    assert any("Implement Agent" in n for n in agent_names), (
        "Implement Agent must be called"
    )


def test_run_iteration_returns_done_when_all_issues_blocked(tmp_path, git_svc, logger):
    """When planning_phase returns AllBlocked (Planner selects zero issues), run_iteration
    returns Done (no improve_mode)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A", "body": "", "comments": []},
        {"number": 2, "title": "Issue B", "body": "", "comments": []},
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

    assert isinstance(result, Done)


def test_run_iteration_all_blocked_ends_iteration_without_improve(
    tmp_path, git_svc, logger
):
    """When planning_phase returns AllBlocked, the iteration ends with Done even in endless
    improve_mode — there is no within-iteration improve fallback from the AllBlocked path."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A", "body": "", "comments": []}
    ]

    improve_dispatched = False

    async def _fake_agent(request: RunRequest):
        nonlocal improve_dispatched
        if request.name == "Plan Agent":
            return _plan_output([])
        # Any other agent call in this iteration would be the improve agent
        improve_dispatched = True
        return CompletionOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(),
            preflight_responses=[[]],
        ),
        improve_mode="endless",
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Done)
    assert not improve_dispatched, (
        "improve must not be dispatched from the AllBlocked path"
    )


def test_run_iteration_improve_chains_into_planning_on_success(
    tmp_path, git_svc, logger
):
    """When dispatcher returns DispatchImprove and improve succeeds (filed issues),
    run_iteration re-fetches open issues and chains into planning, then implement.
    Outcome is Continue."""
    filed_issue = {
        "number": 5,
        "title": "Improve: refactor X",
        "body": "",
        "comments": [],
    }

    github_svc = MagicMock(spec=GithubService)
    # First call (from preflight): no ready-for-agent issues → improve triggers
    # Second call (re-fetch after improve): one new issue filed by improve
    github_svc.get_open_issues.side_effect = [[], [filed_issue]]
    github_svc.get_all_open_issues_lightweight.return_value = []

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output([filed_issue])
        return CompletionOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(),
            preflight_responses=[[]],
        ),
        improve_mode="endless",
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert github_svc.get_open_issues.call_count == 2, (
        "open issues must be re-fetched after improve before planning"
    )


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
        {"number": 1, "title": "Fix A", "body": "", "comments": []},
        {"number": 2, "title": "Fix B", "body": "", "comments": []},
        {"number": 3, "title": "Fix C", "body": "", "comments": []},
    ]
    github_svc.get_open_issues.return_value = open_issues
    recording_runner = FakeAgentRunner(
        [
            _plan_output([{"number": 1, "title": "Fix A", "body": "", "comments": []}]),
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
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]

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

    assert ("remove", "Preflight", "failed", "error") in recording.calls


def test_run_iteration_plan_row_removed_even_if_planning_raises(
    tmp_path, git_svc, logger
):
    """run_iteration must remove the 'Plan' display row even when planning_phase raises."""
    recording = RecordingStatusDisplay()

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A", "body": "", "comments": []},
        {"number": 2, "title": "Issue B", "body": "", "comments": []},
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

    assert ("remove", "Plan", "failed", "error") in recording.calls


def test_run_iteration_implement_row_removed_on_usage_limit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must remove the 'Implement' display row even when usage limit is hit."""
    recording = RecordingStatusDisplay()

    async def _usage_limit(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
        raise UsageLimitError(reset_time=None)

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
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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


def test_run_iteration_registers_preflight_row_with_running_phase(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must register the 'Preflight' row with initial_phase='Running'."""
    recording = RecordingStatusDisplay()

    async def _noop_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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

    assert ("register", "Preflight", "phase", "started", "Running") in recording.calls


def test_run_iteration_registers_plan_row_with_planning_phase(
    tmp_path, git_svc, logger
):
    """run_iteration must register the 'Plan' row with initial_phase='Planning'."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A", "body": "", "comments": []},
        {"number": 2, "title": "Issue B", "body": "", "comments": []},
    ]
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Issue A", "body": "", "comments": []}]
            )
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

    assert (
        "register",
        "Plan",
        "phase",
        "started planning for 2 issue(s) labeled ready-for-agent",
        "Planning",
    ) in recording.calls


def test_run_iteration_plan_row_startup_message_uses_configured_issue_label(
    tmp_path, git_svc, logger
):
    """Plan row startup message uses deps.cfg.issue_label, not a hardcoded string."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A", "body": "", "comments": []},
        {"number": 2, "title": "Issue B", "body": "", "comments": []},
    ]
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Issue A", "body": "", "comments": []}]
            )
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
        cfg=Config(max_parallel=4, max_iterations=1, issue_label="my-custom-label"),
    )
    asyncio.run(run_iteration(deps))

    assert (
        "register",
        "Plan",
        "phase",
        "started planning for 2 issue(s) labeled my-custom-label",
        "Planning",
    ) in recording.calls


def test_run_iteration_registers_implement_row_with_running_phase(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must register the 'Implement' row with initial_phase='Running'."""
    recording = RecordingStatusDisplay()

    async def _noop_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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

    assert ("register", "Implement", "phase", "started", "Running") in recording.calls


# ── Planning skip when in-flight branches or worktrees exist ─────────────────


def test_run_iteration_skips_planning_when_all_issues_have_existing_branches(
    tmp_path, git_svc, logger
):
    """When all open issues have an existing branch, planning_phase is not invoked
    and the iteration proceeds with those issues as the working set."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix A", "body": "", "comments": []},
        {"number": 2, "title": "Fix B", "body": "", "comments": []},
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
        {"number": 3, "title": "Fix C", "body": "", "comments": []},
        {"number": 4, "title": "Fix D", "body": "", "comments": []},
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
        {"number": 5, "title": "In flight", "body": "", "comments": []},
        {"number": 6, "title": "Deferred", "body": "", "comments": []},
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
    github_svc.get_open_issues.return_value = [
        {"number": 7, "title": "In flight", "body": "", "comments": []}
    ]
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
        {"number": 8, "title": "Branch only", "body": "", "comments": []},
        {"number": 9, "title": "Worktree only", "body": "", "comments": []},
        {"number": 10, "title": "Deferred", "body": "", "comments": []},
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


# ── Plan phase row.close() message ────────────────────────────────────────────


def test_run_iteration_plan_close_message_contains_issue_details(
    tmp_path, git_svc, logger
):
    """Plan phase row.close() emits 'Planning complete, implementing N issue(s):' with each issue on a sub-line."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 3, "title": "Issue A", "body": "", "comments": []},
        {"number": 7, "title": "Issue B", "body": "", "comments": []},
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 3, "title": "Issue A", "body": "", "comments": []}]
            )
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

    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must be removed"
    msg = plan_removes[0][2]
    assert "Planning complete, implementing 1 issue(s):" in msg
    assert "#3: Issue A → pycastle/issue-3" in msg


def test_run_iteration_implement_close_message_success_format(
    tmp_path, git_svc, github_svc, logger
):
    """Implement row close message on success is 'Execution complete, N branch(es) with commits:\n  branch'."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
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

    impl_removes = [
        c for c in recording.calls if c[0] == "remove" and c[1] == "Implement"
    ]
    assert impl_removes, "Implement row must be removed"
    msg, style = impl_removes[0][2], impl_removes[0][3]
    assert "Execution complete, 1 branch(es) with commits:" in msg
    assert "pycastle/issue-1" in msg
    assert style == "success"


def test_run_iteration_no_commits_close_uses_warning_style(
    tmp_path, git_svc, github_svc, logger
):
    """Implement row close on no-commits path uses shutdown_style='warning'."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
        raise PromiseParseError("no promise tag")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    impl_removes = [
        c for c in recording.calls if c[0] == "remove" and c[1] == "Implement"
    ]
    assert impl_removes, "Implement row must be removed"
    msg, style = impl_removes[0][2], impl_removes[0][3]
    assert "No commits produced. Nothing to merge." in msg
    assert style == "warning"


def test_run_iteration_preflight_failure_errors_use_implement_caller(
    tmp_path, git_svc, github_svc, logger
):
    """PreflightFailure error lines must be printed with caller='Implement'."""
    from pycastle.agent_result import PreflightFailure as PF

    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
        return PF(failures=(("ruff", "ruff check .", "E501"),))

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    error_prints = [
        c
        for c in recording.calls
        if c[0] == "print" and c[1] == "Implement" and "pre-flight failed" in str(c[2])
    ]
    assert error_prints, (
        "PreflightFailure message must be printed with caller='Implement'"
    )


def test_run_iteration_generic_error_uses_implement_caller(
    tmp_path, git_svc, github_svc, logger
):
    """Generic implement errors must be printed with caller='Implement'."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix bug", "body": "", "comments": []}]
            )
        raise PromiseParseError("bad output")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    error_prints = [
        c
        for c in recording.calls
        if c[0] == "print" and c[1] == "Implement" and "failed" in str(c[2])
    ]
    assert error_prints, "Generic error message must be printed with caller='Implement'"


def test_run_iteration_hitl_message_uses_preflight_caller(tmp_path, git_svc, logger):
    """'Preflight issue requires human intervention' must be printed with caller='Preflight'."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": "", "comments": []}
    ]

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

    hitl_prints = [
        c
        for c in recording.calls
        if c[0] == "print" and c[1] == "Preflight" and "human intervention" in str(c[2])
    ]
    assert hitl_prints, "HITL message must be printed with caller='Preflight'"


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_run_iteration_plan_close_message_when_all_blocked(tmp_path, git_svc, logger):
    """When the planner returns no issues (AllBlocked), Plan row closes with the all-blocked message."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Issue A", "body": "", "comments": []},
        {"number": 2, "title": "Issue B", "body": "", "comments": []},
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
        status_display=recording,
    )
    asyncio.run(run_iteration(deps))

    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must be removed"
    assert "All ready-for-agent issues are blocked" in plan_removes[0][2]


def test_run_iteration_implement_success_message_includes_all_branches(
    tmp_path, git_svc, logger
):
    """When multiple issues complete, every branch name appears in the Implement close message."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 5, "title": "Issue Five", "body": "", "comments": []},
        {"number": 6, "title": "Issue Six", "body": "", "comments": []},
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {"number": 5, "title": "Issue Five", "body": "", "comments": []},
                    {"number": 6, "title": "Issue Six", "body": "", "comments": []},
                ]
            )
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

    impl_removes = [
        c
        for c in recording.calls
        if c[0] == "remove"
        and c[1] == "Implement"
        and "Execution complete" in str(c[2])
    ]
    assert impl_removes, "Implement row must close with success message"
    msg = impl_removes[0][2]
    assert "2 branch(es) with commits:" in msg
    assert "pycastle/issue-5" in msg
    assert "pycastle/issue-6" in msg


def test_run_iteration_success_close_excludes_failed_branches(
    tmp_path, git_svc, logger
):
    """When some issues fail and others succeed, only completed branches appear in the close message."""
    from pycastle.agent_result import PreflightFailure as PF

    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 3, "title": "Issue Three", "body": "", "comments": []},
        {"number": 4, "title": "Issue Four", "body": "", "comments": []},
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {"number": 3, "title": "Issue Three", "body": "", "comments": []},
                    {"number": 4, "title": "Issue Four", "body": "", "comments": []},
                ]
            )
        if request.name == "Implement Agent #3":
            return PF(failures=(("ruff", "ruff check .", "E501"),))
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

    impl_removes = [
        c
        for c in recording.calls
        if c[0] == "remove"
        and c[1] == "Implement"
        and "Execution complete" in str(c[2])
    ]
    assert impl_removes, "Implement row must close with success message"
    msg = impl_removes[0][2]
    assert "1 branch(es) with commits:" in msg
    assert "pycastle/issue-4" in msg
    assert "pycastle/issue-3" not in msg


# ── Improve mode: stop semantics matrix ──────────────────────────────────────
#
# The matrix tests verify that run_iteration applies the correct stop logic for
# every combination of improve_mode × slept_once × improve-phase outcome.


def _make_improve_deps(
    tmp_path,
    git_svc,
    logger,
    *,
    improve_mode,
    slept_once=False,
    agent_responses,
):
    """Return Deps wired for an improve-mode test (0 open AFK issues)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    response_queue = list(agent_responses)

    async def _agent(request: RunRequest):
        return response_queue.pop(0)

    return dataclasses.replace(
        _make_deps(
            tmp_path,
            _agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(),
        ),
        improve_mode=improve_mode,
        slept_once=slept_once,
    )


def test_run_iteration_endless_dispatches_improve_when_idle(tmp_path, git_svc, logger):
    """endless + 0 AFK + not slept → improve dispatched, iteration returns Continue."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        agent_responses=[CompletionOutput(), CompletionOutput(), CompletionOutput()],
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, Continue)


def test_run_iteration_until_sleep_exits_when_slept_and_idle(tmp_path, git_svc, logger):
    """until_sleep + slept_once=True + 0 AFK → Done without dispatching improve."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="until_sleep",
        slept_once=True,
        agent_responses=[],  # no agent calls expected
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, Done)


def test_run_iteration_until_sleep_dispatches_improve_before_first_sleep(
    tmp_path, git_svc, logger
):
    """until_sleep + slept_once=False + 0 AFK → improve dispatched, returns Continue."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="until_sleep",
        slept_once=False,
        agent_responses=[CompletionOutput(), CompletionOutput(), CompletionOutput()],
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, Continue)


def test_run_iteration_endless_dispatches_improve_even_after_sleep(
    tmp_path, git_svc, logger
):
    """endless + slept_once=True + 0 AFK → improve dispatched, returns Continue (slept ignored)."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=True,
        agent_responses=[CompletionOutput(), CompletionOutput(), CompletionOutput()],
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, Continue)


def test_run_iteration_returns_no_candidate_after_rejection_report_filed(
    tmp_path, git_svc, logger
):
    """endless + NO-CANDIDATE improve + report filed → NoCandidate (stops the loop)."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        # scan → NO-CANDIDATE, then report phase → COMPLETE
        agent_responses=[NoCandidateOutput(), CompletionOutput()],
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, NoCandidate)


def test_run_iteration_returns_no_candidate_when_report_disabled(
    tmp_path, git_svc, logger
):
    """endless + NO-CANDIDATE + report disabled → NoCandidate (scan terminates immediately)."""
    base = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        agent_responses=[NoCandidateOutput()],
    )
    deps = dataclasses.replace(
        base,
        cfg=dataclasses.replace(base.cfg, diagnose_on_failure=False),
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, NoCandidate)


def test_run_iteration_returns_no_candidate_in_until_sleep_mode(
    tmp_path, git_svc, logger
):
    """until_sleep + not slept + NO-CANDIDATE → NoCandidate (does not loop again)."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="until_sleep",
        slept_once=False,
        agent_responses=[NoCandidateOutput(), CompletionOutput()],
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, NoCandidate)


def test_run_iteration_successful_improve_still_returns_continue(
    tmp_path, git_svc, logger
):
    """endless + successful improve (picked path) → Continue (normal loop continues)."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        agent_responses=[CompletionOutput(), CompletionOutput(), CompletionOutput()],
    )
    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, Continue)


def test_run_iteration_improve_dispatch_runs_preflight_checks_with_no_open_issues(
    tmp_path, git_svc, logger
):
    """When improve is dispatched with no open issues, PREFLIGHT_CHECKS must run
    before improve-sandbox is created — the improve agent must run against a verified safe SHA."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        agent_responses=[CompletionOutput(), CompletionOutput(), CompletionOutput()],
    )
    asyncio.run(run_iteration(deps))

    assert len(deps.agent_runner.preflight_calls) >= 1, (
        "PREFLIGHT_CHECKS must run before improve agent is dispatched"
    )


def test_run_iteration_improve_uses_sha_from_preflight(tmp_path, git_svc, logger):
    """improve_phase must receive the SHA pinned by preflight, not re-fetch HEAD."""
    git_svc.get_head_sha.return_value = "safe-sha-from-preflight"
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        agent_responses=[CompletionOutput(), CompletionOutput(), CompletionOutput()],
    )
    asyncio.run(run_iteration(deps))

    worktree_shas = {
        c.args[3]
        for c in git_svc.create_worktree.call_args_list
        if len(c.args) > 3 and c.args[3] is not None
    }
    assert "safe-sha-from-preflight" in worktree_shas, (
        "improve-sandbox must be created from the preflight SHA"
    )


def test_run_iteration_returns_aborted_usage_limit_when_improve_agent_hits_limit(
    tmp_path, git_svc, logger
):
    """run_iteration returns AbortedUsageLimit when the improve agent hits the usage limit
    instead of propagating UsageLimitError to the auto bug reporter."""
    from datetime import datetime

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
    reset_time = datetime(2026, 5, 8, 16, 0)

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError(reset_time=reset_time)

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(),
        ),
        improve_mode="endless",
        slept_once=False,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert result.reset_time == reset_time


# ── Centralized UsageLimitError → AbortedUsageLimit conversion ───────────────


@pytest.mark.parametrize(
    "phase",
    [
        "preflight",
        "plan",
        "improve",
        "merge",
    ],
)
def test_run_iteration_returns_aborted_usage_limit_for_each_single_agent_phase(
    tmp_path, git_svc, logger, phase
):
    """run_iteration returns AbortedUsageLimit for each single-agent phase when it hits
    the usage limit. Adding a fifth single-agent phase requires one new parameter row."""
    from datetime import datetime

    reset_time = datetime(2026, 5, 8, 16, 0)
    github_svc = MagicMock(spec=GithubService)

    if phase == "preflight":
        github_svc.get_open_issues.return_value = [
            {"number": 1, "title": "Fix", "body": "", "comments": []}
        ]

        async def agent_fn(req: RunRequest):
            raise UsageLimitError(reset_time=reset_time)

        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[(("ruff", "ruff check .", "E501"),)],
        )
    elif phase == "plan":
        github_svc.get_open_issues.return_value = [
            {"number": 1, "title": "Fix", "body": "", "comments": []}
        ]

        async def agent_fn(req: RunRequest):
            raise UsageLimitError(reset_time=reset_time)

        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[]],
        )
    elif phase == "improve":
        github_svc.get_open_issues.return_value = []

        async def agent_fn(req: RunRequest):
            raise UsageLimitError(reset_time=reset_time)

        deps = dataclasses.replace(
            _make_deps(
                tmp_path,
                agent_fn,
                git_svc=git_svc,
                github_svc=github_svc,
                logger=logger,
                preflight_responses=[[]],
            ),
            improve_mode="endless",
        )
    else:  # merge
        github_svc.get_open_issues.return_value = [
            {"number": 1, "title": "Fix", "body": "", "comments": []}
        ]
        git_svc.try_merge.return_value = False  # force conflict path → Merge Agent

        async def agent_fn(req: RunRequest):
            if req.name == "Plan Agent":
                return _plan_output(
                    [{"number": 1, "title": "Fix", "body": "", "comments": []}]
                )
            if req.name == "Merge Agent":
                raise UsageLimitError(reset_time=reset_time)
            return CompletionOutput()

        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[]],
        )

    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert result.reset_time == reset_time


def test_phase_row_paints_interrupted_style_on_usage_limit(tmp_path, git_svc, logger):
    """When UsageLimitError propagates through a phase_row, the row is removed with
    style 'interrupted' and message 'usage limit reached'."""
    from datetime import datetime

    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix", "body": "", "comments": []}
    ]
    reset_time = datetime(2026, 5, 8, 16, 0)

    async def agent_fn(req: RunRequest):
        raise UsageLimitError(reset_time=reset_time)

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[]],
        status_display=recording,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert ("remove", "Plan", "usage limit reached", "interrupted") in recording.calls


# ── AbortedAgentFailure: FailedOutput recovery ────────────────────────────────


def test_run_iteration_returns_aborted_agent_failure_when_improve_agent_fails(
    tmp_path, git_svc, logger
):
    """When improve agent emits FAILED and diagnose_on_failure is on, run_iteration
    spawns the failure-report agent and returns AbortedAgentFailure with issue_number."""
    response_queue = [
        FailedOutput(),
        IssueOutput(number=42, labels=["bug", "needs-triage"]),
    ]

    async def agent_fn(req: RunRequest):
        return response_queue.pop(0)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(),
        ),
        improve_mode="endless",
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.issue_number == 42
    assert result.failed_role == "improve"


def test_run_iteration_aborted_agent_failure_without_recovery_when_diagnose_disabled(
    tmp_path, git_svc, logger
):
    """When diagnose_on_failure is off and improve agent emits FAILED, no recovery
    agent is spawned and AbortedAgentFailure.issue_number is None."""
    response_queue = [FailedOutput()]

    async def agent_fn(req: RunRequest):
        return response_queue.pop(0)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    base = dataclasses.replace(
        _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(diagnose_on_failure=False),
        ),
        improve_mode="endless",
    )
    result = asyncio.run(run_iteration(base))

    assert isinstance(result, AbortedAgentFailure)
    assert result.issue_number is None
    assert result.failed_role == "improve"


def test_run_iteration_failure_report_receives_correct_run_request(
    tmp_path, git_svc, logger
):
    """Recovery RunRequest has FAILURE_REPORT role, the improve-sandbox worktree path,
    and scope_args with FAILED_ROLE and SESSION_DIR."""
    calls: list[RunRequest] = []
    response_queue = [FailedOutput(), IssueOutput(number=99, labels=["bug"])]

    async def agent_fn(req: RunRequest):
        calls.append(req)
        return response_queue.pop(0)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(),
        ),
        improve_mode="endless",
    )
    asyncio.run(run_iteration(deps))

    assert len(calls) == 2
    failure_req = calls[1]
    assert failure_req.role == AgentRole.FAILURE_REPORT
    expected_wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    assert failure_req.mount_path == expected_wt
    assert failure_req.scope_args is not None
    assert failure_req.scope_args["FAILED_ROLE"] == "improve"
    assert "SESSION_DIR" in failure_req.scope_args
