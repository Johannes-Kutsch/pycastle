import asyncio
import dataclasses
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.config import Config, StageOverride
from pycastle.services import GitService
from pycastle.services import GithubService
from pycastle.provider_errors import ProviderErrorObservation
from pycastle.iteration import (
    AbortedAgentFailure,
    AbortedAgentCredentialFailure,
    AbortedHardApiError,
    AbortedHITL,
    AbortedSetup,
    AbortedTimeout,
    AbortedUsageLimit,
    Continue,
    Done,
    NoCandidate,
    run_iteration,
)
from pycastle.iteration.merge import merge_phase
from pycastle.iteration.preflight import PreflightCache
from pycastle.agents.runner import RunRequest
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.iteration._deps import (
    Deps,
)
from pycastle.agents.output_protocol import (
    AgentRole,
    CommitMessageOutput,
    CompletionOutput,
    IssueOutput,
    FailedOutput,
    NoCandidateOutput,
    PlannerOutput,
    PromiseParseError,
    process_stream,
)
from tests.support import (
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
    _make_deps as _make_test_deps,
)


def _preflight_failure(
    check_name: str, command: str, output: str
) -> PreflightCommandFailure:
    return PreflightCommandFailure(
        check_name=check_name,
        command=command,
        output=output,
    )


def _make_agent_failed_error(role: AgentRole, worktree_path: Path) -> AgentFailedError:
    return AgentFailedError(
        role_value=role.value,
        worktree_path=worktree_path,
        namespace="",
        failure_class="",
    )


def _plan_output(issues: list[dict]) -> PlannerOutput:
    return PlannerOutput(
        issues=[
            {
                "number": i["number"],
                "title": i["title"],
                "labels": i.get("labels", ["behavior-slice"]),
            }
            for i in issues
        ]
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
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
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
    return _make_test_deps(
        tmp_path,
        run_agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=cfg or Config(max_parallel=4, max_iterations=1),
        logger=logger,
        status_display=status_display,
        preflight_responses=[[]]
        if preflight_responses is None
        else preflight_responses,
        preflight_cache=PreflightCache(),
        setup_worktrees=True,
    )


# ── Initial issue fetch ───────────────────────────────────────────────────────


def test_run_iteration_fetches_open_issues_and_all_open_issues_before_preflight(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must call get_open_issues and get_all_open_issues_lightweight
    once before the Preflight phase on each iteration."""

    async def _noop_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
            )
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _noop_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(run_iteration(deps))

    github_svc.get_open_issues.assert_called()
    github_svc.get_all_open_issues_lightweight.assert_called()


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
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=42, labels=["ready-for-human"])

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHITL)
    assert result.issue_number == 42


def test_run_iteration_returns_setup_abort_when_preflight_setup_fails(
    tmp_path, git_svc, github_svc, logger
):
    """A Setup-phase preflight failure aborts before check diagnosis begins."""
    deps = _make_deps(
        tmp_path,
        lambda request: CompletionOutput(),
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[SetupPhaseError("preflight", "pip install failed")],
    )

    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedSetup)
    assert result.phase == "preflight"
    assert "pip install failed" in result.message
    assert deps.agent_runner.calls == []


def test_run_iteration_aborted_hitl_carries_issue_number(tmp_path, git_svc, logger):
    """AbortedHITL must carry the issue number filed by the preflight-issue agent."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=99, labels=["ready-for-human"])

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[
            [_preflight_failure("mypy", "mypy .", "error: Missing module")]
        ],
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
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=7, labels=["ready-for-human"])

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
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
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
            )
        raise UsageLimitError(reset_time=None)

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, AbortedUsageLimit)


# ── AbortedUsageLimit: auto-file parse failures ───────────────────────────────


@pytest.fixture
def github_svc_two_issues():
    svc = MagicMock(spec=GithubService)
    svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    svc.get_all_open_issues_lightweight.return_value = []
    return svc


@pytest.fixture(autouse=True)
def _clear_usage_limit_dedupe(monkeypatch):
    """Reset the per-process dedupe set so tests start with a clean cache."""
    import pycastle.iteration as _iter_mod

    monkeypatch.setattr(_iter_mod, "_FILED_USAGE_LIMIT_RAW_MESSAGES", set())


def test_run_iteration_files_issue_when_usage_limit_has_raw_message(
    tmp_path, git_svc, github_svc_two_issues, logger
):
    """When UsageLimitError.raw_message is non-None, run_iteration calls auto_file_issue
    with a title scoped to the originating provider and a body containing the raw message."""
    raw = "You're out of extra usage · no reset info"

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError(reset_time=None, raw_message=raw, provider="claude")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc_two_issues,
        logger=logger,
        preflight_responses=[[]],
    )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    mock_file.assert_called_once()
    title = mock_file.call_args[0][0]
    body = mock_file.call_args[0][1]
    assert title == "[pycastle] failed to parse usage-limit reset time (claude)"
    assert raw in body


def test_run_iteration_dedupes_auto_file_on_same_raw_message(
    tmp_path, git_svc, github_svc_two_issues, logger
):
    """Multiple UsageLimitErrors with the same raw_message fire auto_file_issue only once."""
    raw = "You're out of extra usage · same message repeated"

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError(reset_time=None, raw_message=raw, provider="claude")

    def _deps():
        return _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc_two_issues,
            logger=logger,
            preflight_responses=[[]],
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        asyncio.run(run_iteration(_deps()))
        asyncio.run(run_iteration(_deps()))

    assert mock_file.call_count == 1


def test_run_iteration_does_not_file_issue_when_raw_message_is_none(
    tmp_path, git_svc, github_svc, logger
):
    """When UsageLimitError.raw_message is None (successful parse), auto_file_issue is not called."""

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
            )
        raise UsageLimitError(reset_time=None, raw_message=None)

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    mock_file.assert_not_called()


def test_run_iteration_files_issue_with_codex_provider(
    tmp_path, git_svc, github_svc_two_issues, logger
):
    """Provider identity is reflected in the title: (codex) when provider='codex'."""
    raw = "You've hit your usage limit, try again later"

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError(reset_time=None, raw_message=raw, provider="codex")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc_two_issues,
        logger=logger,
        preflight_responses=[[]],
    )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    title = mock_file.call_args[0][0]
    assert title == "[pycastle] failed to parse usage-limit reset time (codex)"


def test_run_iteration_still_returns_aborted_usage_limit_after_filing(
    tmp_path, git_svc, github_svc_two_issues, logger
):
    """run_iteration returns AbortedUsageLimit even when auto_file_issue fires."""
    raw = "Usage limit hit, parse failed"
    reset = datetime(2026, 5, 19, 13, 0)

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError(reset_time=reset, raw_message=raw, provider="claude")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc_two_issues,
        logger=logger,
        preflight_responses=[[]],
    )

    with patch("pycastle.iteration.auto_file_issue"):
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert result.reset_time == reset


# ── Continue: normal iteration completion ─────────────────────────────────────


def test_run_iteration_returns_continue_when_issues_complete_normally(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns Continue after a normal plan→implement→merge cycle."""

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
            )
        raise PromiseParseError("no <promise>COMPLETE</promise> tag")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


# ── Preflight: AFK verdict ────────────────────────────────────────────────────


def test_run_iteration_returns_continue_on_afk_preflight_verdict(
    tmp_path, git_svc, logger
):
    """run_iteration returns Continue when preflight fails with an AFK verdict.
    The filed fix issue is implemented in the same iteration without a plan step."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    github_svc.get_issue.return_value = {
        "number": 55,
        "title": "Preflight fix",
        "body": "x" * 100,
        "labels": ["behavior-slice"],
    }

    async def _fake_agent(request: RunRequest):
        if "Pre-Flight Reporter" in request.name:
            return IssueOutput(number=55, labels=["ready-for-agent", "behavior-slice"])
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


# ── StatusDisplay routing ──────────────────────────────────────────────────────


def test_run_iteration_routes_planning_complete_through_status_display(
    tmp_path, git_svc, logger, capsys
):
    """run_iteration must route the planning-complete summary through status_display (as the Plan row close message)."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Issue A",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
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
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
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
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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
        {
            "number": 3,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 7,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 3,
                        "title": "Issue A",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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


def test_run_iteration_single_issue_skips_plan_agent_and_still_implements(
    tmp_path, git_svc, logger
):
    """With exactly one open issue (not in-flight), planning_phase skips the planner
    and the iteration proceeds directly to implement."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 7,
            "title": "Single issue",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

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
        "Plan Agent must be skipped for a single issue"
    )
    assert any("Implement Agent" in n for n in agent_names), (
        "Implement Agent must still run"
    )


def test_run_iteration_returns_done_when_all_issues_blocked(tmp_path, git_svc, logger):
    """When planning_phase returns AllBlocked (Planner selects zero issues), run_iteration
    returns Done (no improve_mode)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
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
        {
            "number": 1,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
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
    """When the improve gate dispatches and improve succeeds (filed issues),
    run_iteration re-fetches open issues and chains into planning, then implement.
    Outcome is Continue."""
    filed_issue = {
        "number": 5,
        "title": "Improve: refactor X",
        "body": "x" * 100,
        "comments": [],
        "labels": ["refactor-slice"],
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


def test_implementer_work_body_includes_slice_mode_for_behavior_slice(
    tmp_path, git_svc, github_svc, logger
):
    issue_title = "Fix auth bug"
    github_svc.get_open_issues.return_value = [
        {
            "number": 3,
            "title": issue_title,
            "body": "x" * 100,
            "labels": ["behavior-slice"],
        }
    ]
    recording_runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput()],
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
    assert len(implementer_calls) == 1
    assert implementer_calls[0].work_body == f'implementing behavior "{issue_title}"'


def test_implementer_and_reviewer_run_calls_pass_work_body_with_issue_title(
    tmp_path, git_svc, github_svc, logger
):
    issue_title = "Fix auth bug"
    # Single issue: planning skips, so no Plan Agent call. Queue only needs implement+review.
    github_svc.get_open_issues.return_value = [
        {
            "number": 3,
            "title": issue_title,
            "body": "x" * 100,
            "labels": ["behavior-slice"],
        }
    ]
    recording_runner = FakeAgentRunner(
        [
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
    assert implementer_calls[0].work_body == f'implementing behavior "{issue_title}"'
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0].work_body == f'reviewing behavior "{issue_title}"'


@pytest.mark.parametrize(
    "label,mode",
    [
        ("refactor-slice", "refactor"),
        ("docs-slice", "docs"),
    ],
)
def test_implementer_and_reviewer_work_body_includes_slice_mode(
    tmp_path, git_svc, github_svc, logger, label, mode
):
    issue_title = "Some task"
    github_svc.get_open_issues.return_value = [
        {"number": 5, "title": issue_title, "body": "x" * 100, "labels": [label]}
    ]
    recording_runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput()],
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
    assert implementer_calls[0].work_body == f'implementing {mode} "{issue_title}"'
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0].work_body == f'reviewing {mode} "{issue_title}"'


def test_planner_run_call_passes_work_body_with_issue_count(
    tmp_path, git_svc, github_svc, logger
):
    open_issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 3,
            "title": "Fix C",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    github_svc.get_open_issues.return_value = open_issues
    recording_runner = FakeAgentRunner(
        [
            _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Fix A",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
            ),
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


def test_run_iteration_plan_row_removed_even_if_planning_raises(
    tmp_path, git_svc, logger
):
    """run_iteration must remove the 'Plan' display row even when planning_phase raises."""
    recording = RecordingStatusDisplay()

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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


def test_run_iteration_registers_plan_row_with_planning_phase(
    tmp_path, git_svc, logger
):
    """run_iteration must register the 'Plan' row with initial_phase='Planning'."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Issue A",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
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

    assert (
        "register",
        "Plan",
        "phase",
        "started planning for 2 issue(s) labeled ready-for-agent",
        "Planning",
        None,
    ) in recording.calls


def test_run_iteration_plan_row_startup_message_uses_configured_issue_label(
    tmp_path, git_svc, logger
):
    """Plan row startup message uses deps.cfg.issue_label, not a hardcoded string."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Issue A",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
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
        cfg=Config(max_parallel=4, max_iterations=1, issue_label="my-custom-label"),
    )
    asyncio.run(run_iteration(deps))

    assert (
        "register",
        "Plan",
        "phase",
        "started planning for 2 issue(s) labeled my-custom-label",
        "Planning",
        None,
    ) in recording.calls


def test_run_iteration_registers_implement_row_with_running_phase(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must register the 'Implement' row with initial_phase='Running'."""
    recording = RecordingStatusDisplay()

    async def _noop_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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

    assert (
        "register",
        "Implement",
        "phase",
        "started",
        "Running",
        None,
    ) in recording.calls


# ── In-flight selector integration ───────────────────────────────────────────


def test_run_iteration_passes_full_ready_for_agent_fetch_to_in_flight_selector(
    tmp_path, git_svc, logger
):
    """The initial ready-for-agent fetch is classified once as a whole list before planning."""
    github_svc = MagicMock(spec=GithubService)
    issues = [
        {
            "number": 5,
            "title": "In flight",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 6,
            "title": "Also in flight",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    github_svc.get_open_issues.return_value = issues

    agent_names: list[str] = []

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    def _selector(candidates, *, repo_root, git_svc):
        del repo_root, git_svc
        if [issue["number"] for issue in candidates] == [5, 6]:
            return list(candidates)
        return []

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with patch("pycastle.iteration.select_in_flight_issues", side_effect=_selector):
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert "Plan Agent" not in agent_names, (
        "Plan Agent must be skipped when the selector classifies the full fetched list"
    )
    assert "Implement Agent #5" in agent_names
    assert "Implement Agent #6" in agent_names


def test_run_iteration_selected_in_flight_issues_resume_through_planning(
    tmp_path, git_svc, logger
):
    """Selected in-flight issues still traverse planning's preflight gate and skip Plan Agent."""
    github_svc = MagicMock(spec=GithubService)
    issues = [
        {
            "number": 5,
            "title": "In flight",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 6,
            "title": "Also in flight",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    github_svc.get_open_issues.return_value = issues
    recording = RecordingStatusDisplay()
    agent_names: list[str] = []
    call_count = 0

    class _SequentialCache:
        async def get_safe_sha(self, deps):
            del deps
            nonlocal call_count
            call_count += 1
            from pycastle.iteration.preflight import PreflightReady

            return PreflightReady(sha="sha-x1")

    async def _fake_agent(request: RunRequest):
        agent_names.append(request.name)
        return CompletionOutput()

    def _selector(candidates, *, repo_root, git_svc):
        del repo_root, git_svc
        if [issue["number"] for issue in candidates] == [5, 6]:
            return list(candidates)
        return []

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            status_display=recording,
        ),
        preflight_cache=_SequentialCache(),  # type: ignore[arg-type]
    )

    with patch("pycastle.iteration.select_in_flight_issues", side_effect=_selector):
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert call_count == 1, (
        "selected in-flight issues must still use planning's preflight gate exactly once"
    )
    assert "Plan Agent" not in agent_names
    assert "Implement Agent #5" in agent_names
    assert "Implement Agent #6" in agent_names
    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must close on the in-flight resume path"
    assert "resuming 2 in-flight branch(es) (#5, #6)" in plan_removes[0][2]
    assert "skipping plan agent" in plan_removes[0][2]
    implementer_sha = git_svc.create_worktree.call_args_list[0][0][3]
    assert implementer_sha == "sha-x1", (
        "selected in-flight issues must hand planning's SHA to implementation"
    )


# ── [Plan] row rendered on all paths ─────────────────────────────────────────


def test_run_iteration_plan_row_rendered_for_single_afk_issue(
    tmp_path, git_svc, logger
):
    """One open AFK issue + no in-flight: planning skips, [Plan] row appears."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 11,
            "title": "Solo",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

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
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    plan_registers = [
        c for c in recording.calls if c[0] == "register" and c[1] == "Plan"
    ]
    assert plan_registers, "[Plan] row must be registered for single-issue path"
    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "[Plan] row must be removed for single-issue path"
    assert "#11" in plan_removes[0][2], (
        "Close message must mention the skipped issue number"
    )
    assert "skipping plan agent" in plan_removes[0][2]


def test_run_iteration_plan_row_rendered_for_two_afk_issues(tmp_path, git_svc, logger):
    """Two open AFK issues: planner runs, [Plan] row appears with 'Planning complete'."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 3,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 4,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 3,
                        "title": "Issue A",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
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
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    plan_registers = [
        c for c in recording.calls if c[0] == "register" and c[1] == "Plan"
    ]
    assert plan_registers, "[Plan] row must be registered for multi-issue path"
    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "[Plan] row must be removed for multi-issue path"
    assert "Planning complete" in plan_removes[0][2]


# ── Plan phase row.close() message ────────────────────────────────────────────


def test_run_iteration_plan_close_message_contains_issue_details(
    tmp_path, git_svc, logger
):
    """Plan phase row.close() emits 'Planning complete, implementing N issue(s):' with each issue on a sub-line."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 3,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 7,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 3,
                        "title": "Issue A",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
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
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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


def test_run_iteration_generic_error_uses_implement_caller(
    tmp_path, git_svc, github_svc, logger
):
    """Generic implement errors must be printed with caller='Implement'."""
    recording = RecordingStatusDisplay()

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Fix bug",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    }
                ]
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
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
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
        preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
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
        {
            "number": 1,
            "title": "Issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
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
        {
            "number": 5,
            "title": "Issue Five",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 6,
            "title": "Issue Six",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 5,
                        "title": "Issue Five",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    },
                    {
                        "number": 6,
                        "title": "Issue Six",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    },
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
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 3,
            "title": "Issue Three",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 4,
            "title": "Issue Four",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(
                [
                    {
                        "number": 3,
                        "title": "Issue Three",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    },
                    {
                        "number": 4,
                        "title": "Issue Four",
                        "body": "x" * 100,
                        "comments": [],
                        "labels": ["behavior-slice"],
                    },
                ]
            )
        if request.name == "Implement Agent #3":
            raise RuntimeError("agent failed")
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
    """improve_phase pins its worktree via checkout_detached (called by
    PreflightCache.get_safe_sha) using the SHA obtained after pull — not via a
    SHA arg to create_worktree."""
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

    detached_shas = {
        c.args[2] for c in git_svc.checkout_detached.call_args_list if len(c.args) > 2
    }
    assert "safe-sha-from-preflight" in detached_shas, (
        "PreflightCache.get_safe_sha must checkout_detached a worktree to the preflight SHA"
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
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ]

        async def agent_fn(req: RunRequest):
            raise UsageLimitError(reset_time=reset_time)

        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
        )
    elif phase == "plan":
        github_svc.get_open_issues.return_value = [
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Fix B",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
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
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ]
        git_svc.try_merge.return_value = False  # force conflict path → Merge Agent

        async def agent_fn(req: RunRequest):
            if req.name == "Plan Agent":
                return _plan_output(
                    [
                        {
                            "number": 1,
                            "title": "Fix",
                            "body": "x" * 100,
                            "comments": [],
                            "labels": ["behavior-slice"],
                        }
                    ]
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
        {
            "number": 1,
            "title": "Fix",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
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
        _make_agent_failed_error(
            AgentRole.IMPROVE, tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
        ),
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
    response_queue = [
        _make_agent_failed_error(
            AgentRole.IMPROVE, tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
        )
    ]

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
    and dispatch settings from the failure-report path."""
    calls: list[RunRequest] = []
    response_queue = [
        _make_agent_failed_error(
            AgentRole.IMPROVE, tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
        ),
        IssueOutput(number=99, labels=["bug"]),
    ]

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
            cfg=Config(
                preflight_issue_override=StageOverride(service="codex", effort="medium")
            ),
        ),
        improve_mode="endless",
    )
    asyncio.run(run_iteration(deps))

    assert len(calls) == 2
    failure_req = calls[1]
    assert failure_req.role == AgentRole.FAILURE_REPORT
    assert failure_req.template == PromptTemplate.FAILURE_REPORT
    assert failure_req.service == "codex"
    expected_wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    assert failure_req.mount_path == expected_wt


def test_run_iteration_failure_report_crash_logs_warning_and_error(
    tmp_path, git_svc, logger
):
    """When the Failure-Report agent itself crashes, the except block must log a
    status-display warning and write both tracebacks to the logger."""
    original_error = _make_agent_failed_error(
        AgentRole.IMPROVE, tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    )
    report_crash = RuntimeError("report agent exploded")
    response_queue: list = [original_error, report_crash]

    async def agent_fn(req: RunRequest):
        resp = response_queue.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    status = RecordingStatusDisplay()
    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(),
            status_display=status,
        ),
        improve_mode="endless",
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.issue_number is None

    prints = [(c[1], c[2]) for c in status.calls if c[0] == "print"]
    assert any("Failure-Report agent crashed" in str(msg) for _, msg in prints)

    assert len(logger.internal_errors) == 1
    label, logged_error, logged_cause = logger.internal_errors[0]
    assert "role=improve" in label
    assert logged_error is report_crash
    assert logged_cause is original_error


def test_run_iteration_routes_failure_report_credential_failure_through_shared_terminal_path(
    tmp_path, git_svc, logger
):
    original_error = _make_agent_failed_error(
        AgentRole.IMPLEMENTER, tmp_path / "pycastle" / ".worktrees" / "issue-1"
    )
    credential_error = AgentCredentialFailureError(
        "Codex authentication missing: run `codex login` on the host.",
        status_code=401,
        service_name="codex",
        observations=(
            ProviderErrorObservation(
                service_name="codex",
                raw_provider_text=(
                    "Codex authentication missing: run `codex login` on the host."
                ),
                source_stream="pre-dispatch host check",
                status_code=401,
            ),
        ),
    )
    response_queue: list = [original_error, credential_error]

    async def agent_fn(req: RunRequest):
        resp = response_queue.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    github_svc.get_all_open_issues_lightweight.return_value = []
    github_svc.search_open_issues_by_title.return_value = []

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(diagnose_on_failure=True),
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    github_svc.create_issue_in.assert_called_once()
    _owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert "Agent: Failure Report Agent" in body
    assert (
        "Run `codex login` on the host to seed Codex credentials before dispatch."
        in body
    )
    assert labels == ["bug", "needs-triage"]
    assert logger.internal_errors == []


def test_run_iteration_returns_aborted_agent_failure_when_planner_agent_fails(
    tmp_path, git_svc, logger
):
    """Planner FailedOutput with two ready-for-agent issues spawns failure-report and
    returns AbortedAgentFailure(failed_role='planner') with the filed issue number."""
    calls: list[RunRequest] = []
    expected_path = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    response_queue = [
        _make_agent_failed_error(AgentRole.PLANNER, expected_path),
        IssueOutput(number=55, labels=["bug"]),
    ]

    async def agent_fn(req: RunRequest):
        calls.append(req)
        if req.role == AgentRole.FAILURE_REPORT:
            assert req.mount_path == expected_path
            assert (req.mount_path / "pyproject.toml").exists()
        return response_queue.pop(0)

    def checkout_detached(repo: Path, path: Path, sha: str) -> None:
        assert repo == tmp_path
        assert sha == "abc123"
        path.mkdir(parents=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n")

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    git_svc.checkout_detached.side_effect = checkout_detached

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(),
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "planner"
    assert result.issue_number == 55

    assert len(calls) == 2
    failure_req = calls[1]
    assert failure_req.role == AgentRole.FAILURE_REPORT
    assert failure_req.template == PromptTemplate.FAILURE_REPORT
    assert failure_req.mount_path == expected_path
    assert expected_path.exists()


def test_run_iteration_failure_report_dispatches_after_opencode_planner_failure(
    tmp_path, git_svc, logger
):
    calls: list[RunRequest] = []
    expected_path = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    response_queue = [
        FailedOutput(failure_class="protocol_error"),
        IssueOutput(number=55, labels=["bug"]),
    ]

    async def agent_fn(req: RunRequest):
        calls.append(req)
        return response_queue.pop(0)

    def checkout_detached(repo: Path, path: Path, sha: str) -> None:
        assert repo == tmp_path
        assert sha == "abc123"
        path.mkdir(parents=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n")

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    git_svc.checkout_detached.side_effect = checkout_detached

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(plan_override=StageOverride(service="opencode", effort="medium")),
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "planner"
    assert len(calls) == 2
    failure_req = calls[1]
    assert failure_req.role == AgentRole.FAILURE_REPORT
    assert failure_req.template == PromptTemplate.FAILURE_REPORT
    assert failure_req.service == "codex"
    assert failure_req.mount_path == expected_path


def test_run_iteration_returns_aborted_agent_failure_when_implementer_agent_fails(
    tmp_path, git_svc, logger
):
    """Implementer FailedOutput on a single in-flight issue spawns failure-report and
    returns AbortedAgentFailure(failed_role='implementer') with the filed issue number."""
    calls: list[RunRequest] = []
    response_queue = [
        _make_agent_failed_error(
            AgentRole.IMPLEMENTER, tmp_path / "pycastle" / ".worktrees" / "issue-1"
        ),
        IssueOutput(number=77, labels=["bug"]),
    ]

    async def agent_fn(req: RunRequest):
        calls.append(req)
        return response_queue.pop(0)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(),
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "implementer"
    assert result.issue_number == 77

    assert len(calls) == 2
    failure_req = calls[1]
    assert failure_req.role == AgentRole.FAILURE_REPORT
    assert failure_req.template == PromptTemplate.FAILURE_REPORT
    assert failure_req.mount_path == tmp_path / "pycastle" / ".worktrees" / "issue-1"


def test_run_iteration_returns_aborted_agent_failure_when_reviewer_agent_fails(
    tmp_path, git_svc, logger
):
    """Reviewer FailedOutput after a successful implementer spawns failure-report and
    returns AbortedAgentFailure(failed_role='reviewer') with the filed issue number."""
    calls: list[RunRequest] = []
    response_queue = [
        CommitMessageOutput(message="initial impl"),
        _make_agent_failed_error(
            AgentRole.REVIEWER, tmp_path / "pycastle" / ".worktrees" / "issue-1"
        ),
        IssueOutput(number=88, labels=["bug"]),
    ]

    async def agent_fn(req: RunRequest):
        calls.append(req)
        return response_queue.pop(0)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(),
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "reviewer"
    assert result.issue_number == 88

    assert len(calls) == 3
    failure_req = calls[2]
    assert failure_req.role == AgentRole.FAILURE_REPORT
    assert failure_req.template == PromptTemplate.FAILURE_REPORT
    assert failure_req.mount_path == tmp_path / "pycastle" / ".worktrees" / "issue-1"


def test_run_iteration_aborted_agent_failure_without_recovery_when_diagnose_disabled_planner(
    tmp_path, git_svc, logger
):
    """With diagnose_on_failure=False, a planner FAILED yields issue_number=None and
    no recovery RunRequest is dispatched."""
    calls: list[RunRequest] = []
    response_queue = [
        _make_agent_failed_error(
            AgentRole.PLANNER, tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
        )
    ]

    async def agent_fn(req: RunRequest):
        calls.append(req)
        return response_queue.pop(0)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(diagnose_on_failure=False),
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "planner"
    assert result.issue_number is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("role", "worktree_name", "github_issues"),
    [
        (
            AgentRole.PLANNER,
            "plan-sandbox",
            [
                {
                    "number": 1,
                    "title": "Fix A",
                    "body": "x" * 100,
                    "comments": [],
                    "labels": ["behavior-slice"],
                },
                {
                    "number": 2,
                    "title": "Fix B",
                    "body": "x" * 100,
                    "comments": [],
                    "labels": ["behavior-slice"],
                },
            ],
        ),
        (
            AgentRole.IMPLEMENTER,
            "issue-1",
            [
                {
                    "number": 1,
                    "title": "Fix A",
                    "body": "x" * 100,
                    "comments": [],
                    "labels": ["behavior-slice"],
                }
            ],
        ),
    ],
)
def test_run_iteration_preserves_agent_failed_worktree_after_run_ends(
    tmp_path, git_svc, logger, role, worktree_name, github_issues
):
    """AgentFailedError worktrees survive run_iteration for planner startup failures
    and implementer mid-run failures alike."""
    calls: list[RunRequest] = []

    async def agent_fn(req: RunRequest):
        calls.append(req)
        raise _make_agent_failed_error(role, req.mount_path)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = github_issues

    expected_path = tmp_path / "pycastle" / ".worktrees" / worktree_name

    def checkout_detached(repo: Path, path: Path, sha: str) -> None:
        assert repo == tmp_path
        assert sha == "abc123"
        path.mkdir(parents=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n")

    git_svc.checkout_detached.side_effect = checkout_detached

    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(diagnose_on_failure=False),
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert len(calls) == 1
    assert expected_path.exists()


# ── AbortedTimeout: centralized AgentTimeoutError catch ──────────────────────


@pytest.mark.parametrize(
    "phase",
    [
        "preflight",
        "plan",
        "improve",
        "merge",
    ],
)
def test_run_iteration_returns_aborted_timeout_for_each_single_agent_phase(
    tmp_path, git_svc, logger, phase
):
    """run_iteration returns AbortedTimeout for each single-agent phase when it times out.
    Adding a fifth single-agent phase requires one new parameter row."""
    from pycastle.agents.output_protocol import AgentRole

    github_svc = MagicMock(spec=GithubService)

    if phase == "preflight":
        github_svc.get_open_issues.return_value = [
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ]

        async def agent_fn(req: RunRequest):
            raise AgentTimeoutError("timeout")

        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
        )
        expected_role = AgentRole.PREFLIGHT_ISSUE.value
        expected_wt = tmp_path / "pycastle" / ".worktrees" / "preflight-sandbox"
    elif phase == "plan":
        github_svc.get_open_issues.return_value = [
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
            {
                "number": 2,
                "title": "Fix B",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            },
        ]

        async def agent_fn(req: RunRequest):
            raise AgentTimeoutError("timeout")

        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[]],
        )
        expected_role = AgentRole.PLANNER.value
        expected_wt = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    elif phase == "improve":
        github_svc.get_open_issues.return_value = []

        async def agent_fn(req: RunRequest):
            raise AgentTimeoutError("timeout")

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
        expected_role = AgentRole.IMPROVE.value
        expected_wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    else:  # merge
        github_svc.get_open_issues.return_value = [
            {
                "number": 1,
                "title": "Fix",
                "body": "x" * 100,
                "comments": [],
                "labels": ["behavior-slice"],
            }
        ]
        git_svc.try_merge.return_value = False  # force conflict path → Merge Agent

        async def agent_fn(req: RunRequest):
            if req.name == "Plan Agent":
                return _plan_output(
                    [
                        {
                            "number": 1,
                            "title": "Fix",
                            "body": "x" * 100,
                            "comments": [],
                            "labels": ["behavior-slice"],
                        }
                    ]
                )
            if req.name == "Merge Agent":
                raise AgentTimeoutError("timeout")
            return CompletionOutput()

        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[]],
        )
        expected_role = AgentRole.MERGER.value
        expected_wt = tmp_path / "pycastle" / ".worktrees" / "merge-sandbox-issue-1"

    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedTimeout)
    assert result.failed_role == expected_role
    assert result.worktree_path == expected_wt


def test_phase_row_paints_interrupted_style_on_agent_timeout(tmp_path, git_svc, logger):
    """When AgentTimeoutError propagates through a phase_row, the row is removed with
    style 'interrupted' and message 'timed out'."""
    recording = RecordingStatusDisplay()
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def agent_fn(req: RunRequest):
        raise AgentTimeoutError("timeout")

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

    assert isinstance(result, AbortedTimeout)
    assert ("remove", "Plan", "timed out", "interrupted") in recording.calls


def test_run_iteration_aborted_timeout_preserves_worktree_when_session_populated(
    tmp_path, git_svc, logger
):
    """When AbortedTimeout is returned for the improve phase, the role session worktree
    is preserved because any_role_dir_present fires on the populated session dir."""
    from pycastle.agents.output_protocol import AgentRole
    from pycastle.session import RoleSession

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    async def agent_fn(req: RunRequest):
        session = RoleSession(req.mount_path, AgentRole.IMPROVE)
        session.path.mkdir(parents=True, exist_ok=True)
        (session.path / "conversation.jsonl").write_text("{}")
        raise AgentTimeoutError("timeout")

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
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedTimeout)
    assert result.worktree_path.exists()
    session = RoleSession(result.worktree_path, AgentRole.IMPROVE)
    assert session.is_resumable()


# ── Regression #679: implement SHA pinned to planner's SHA ──────────────────


def test_run_iteration_preflight_fix_uses_planner_sha_not_second_call(
    tmp_path, git_svc, logger
):
    """Regression #679: when HEAD advances between planning and implement, the implementer
    worktree must be pinned to the SHA from planning's preflight call, not a re-derived SHA.
    Verified by a sequential stub that would return X2 on a second get_safe_sha call."""
    from pycastle.iteration.preflight import PreflightAFK

    call_count = 0

    class _SequentialCache:
        async def get_safe_sha(self, deps):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return PreflightAFK(sha="sha-x1", issue_number=181)
            return PreflightAFK(sha="sha-x2", issue_number=182)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix bug",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    github_svc.get_issue.return_value = {
        "number": 181,
        "title": "Fix preflight failure",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }

    async def _fake_agent(request: RunRequest):
        return CompletionOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
        ),
        preflight_cache=_SequentialCache(),  # type: ignore[arg-type]
    )
    asyncio.run(run_iteration(deps))

    assert call_count == 1, (
        "get_safe_sha must be called exactly once (from planning_phase)"
    )
    implementer_sha = git_svc.create_worktree.call_args_list[0][0][3]
    assert implementer_sha == "sha-x1", (
        "implementer worktree must be pinned to planning's SHA"
    )


def test_run_iteration_routes_merge_time_preflight_afk_at_iteration_boundary(
    tmp_path, git_svc, logger
):
    """When merge-time preflight files an AFK repair issue, run_iteration must preserve
    clean merge work, then implement only the filed preflight issue from the merge-time
    SHA and return Continue so conflict branches resume in a later iteration."""
    from pycastle.iteration.preflight import PreflightAFK, PreflightReady

    action_log: list[tuple[str, object]] = []
    implemented_issue_numbers: list[int] = []
    call_count = 0

    class _SequentialCache:
        async def get_safe_sha(self, deps):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return PreflightReady(sha="plan-sha")
            return PreflightAFK(sha="merge-sha", issue_number=181)

        async def pull_with_resolution(self, deps):
            return None

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Clean branch",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Conflict branch",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    github_svc.get_all_open_issues_lightweight.return_value = []

    def _get_issue(issue_number: int):
        action_log.append(("get_issue", issue_number))
        assert issue_number == 181
        return {
            "number": 181,
            "title": "Fix merge-time preflight failure",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }

    github_svc.get_issue.side_effect = _get_issue

    def _close_issue(issue_number: int):
        action_log.append(("close_issue", issue_number))

    github_svc.close_issue.side_effect = _close_issue

    def _delete_branch(branch: str, repo_root: Path):
        action_log.append(("delete_branch", branch))

    git_svc.delete_branch.side_effect = _delete_branch
    git_svc.try_merge.side_effect = [True, False, True]

    async def _fake_agent(request: RunRequest):
        action_log.append(("agent", request.name))
        if request.name == "Plan Agent":
            return _plan_output(github_svc.get_open_issues.return_value)
        if request.name.startswith("Implement Agent #"):
            implemented_issue_numbers.append(int(request.name.split("#")[1]))
        return CompletionOutput()

    def _push(repo_root: Path, resolver):
        action_log.append(("push", repo_root))

    git_svc.push.side_effect = _push

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(max_parallel=4, max_iterations=1, auto_push=True),
        ),
        preflight_cache=_SequentialCache(),  # type: ignore[arg-type]
    )

    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert call_count == 2
    assert implemented_issue_numbers == [1, 2, 181]
    pinned_shas_by_branch = {
        call.args[2]: call.args[3]
        for call in git_svc.create_worktree.call_args_list
        if call.args[3] is not None
    }
    assert pinned_shas_by_branch["pycastle/issue-1"] == "plan-sha"
    assert pinned_shas_by_branch["pycastle/issue-2"] == "plan-sha"
    assert pinned_shas_by_branch["pycastle/issue-181"] == "merge-sha"
    closed_issue_numbers = [
        call.args[0] for call in github_svc.close_issue.call_args_list
    ]
    assert closed_issue_numbers == [1, 181]
    deleted_branches = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert deleted_branches == ["pycastle/issue-1", "pycastle/issue-181"]
    assert ("push", tmp_path) in action_log
    assert action_log.index(("push", tmp_path)) < action_log.index(("get_issue", 181))


def test_run_iteration_aborts_on_merge_time_preflight_hitl_at_iteration_boundary(
    tmp_path, git_svc, logger
):
    """When merge-time preflight returns HITL, run_iteration must preserve the clean
    merge, push it when enabled, and abort with the HITL issue number."""
    from pycastle.iteration.preflight import PreflightHITL, PreflightReady

    action_log: list[tuple[str, object]] = []
    implemented_issue_numbers: list[int] = []
    call_count = 0

    class _SequentialCache:
        async def get_safe_sha(self, deps):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return PreflightReady(sha="plan-sha")
            return PreflightHITL(sha="merge-sha", issue_number=182)

        async def pull_with_resolution(self, deps):
            return None

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Clean branch",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Conflict branch",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    github_svc.get_all_open_issues_lightweight.return_value = []
    github_svc.close_issue.side_effect = lambda issue_number: action_log.append(
        ("close_issue", issue_number)
    )

    git_svc.delete_branch.side_effect = lambda branch, repo_root: action_log.append(
        ("delete_branch", branch)
    )
    git_svc.push.side_effect = lambda repo_root, resolver: action_log.append(
        ("push", repo_root)
    )
    git_svc.try_merge.side_effect = [True, False]

    async def _fake_agent(request: RunRequest):
        action_log.append(("agent", request.name))
        if request.name == "Plan Agent":
            return _plan_output(github_svc.get_open_issues.return_value)
        if request.name.startswith("Implement Agent #"):
            implemented_issue_numbers.append(int(request.name.split("#")[1]))
        return CompletionOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(max_parallel=4, max_iterations=1, auto_push=True),
        ),
        preflight_cache=_SequentialCache(),  # type: ignore[arg-type]
    )

    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHITL)
    assert result.issue_number == 182
    assert call_count == 2
    assert implemented_issue_numbers == [1, 2]
    closed_issue_numbers = [
        call.args[0] for call in github_svc.close_issue.call_args_list
    ]
    assert closed_issue_numbers == [1]
    deleted_branches = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert deleted_branches == ["pycastle/issue-1"]
    assert ("push", tmp_path) in action_log


def test_run_iteration_merge_time_preflight_issue_agent_failure_aborts_normally(
    tmp_path, git_svc, logger
):
    """If the preflight-issue agent fails during merge-time preflight, run_iteration
    must follow the normal AbortedAgentFailure path and not continue conflict merges."""
    from pycastle.agents.output_protocol import AgentRole
    from pycastle.iteration.preflight import PreflightReady

    action_log: list[tuple[str, object]] = []
    implemented_issue_numbers: list[int] = []
    call_count = 0
    preflight_path = tmp_path / "pycastle" / ".worktrees" / "preflight-sandbox"

    class _SequentialCache:
        async def get_safe_sha(self, deps):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return PreflightReady(sha="plan-sha")
            preflight_path.mkdir(parents=True, exist_ok=True)
            (preflight_path / "pyproject.toml").write_text("[project]\nname='t'\n")
            raise _make_agent_failed_error(AgentRole.PREFLIGHT_ISSUE, preflight_path)

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Clean branch",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Conflict branch",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    github_svc.get_all_open_issues_lightweight.return_value = []
    github_svc.close_issue.side_effect = lambda issue_number: action_log.append(
        ("close_issue", issue_number)
    )

    git_svc.delete_branch.side_effect = lambda branch, repo_root: action_log.append(
        ("delete_branch", branch)
    )
    git_svc.try_merge.side_effect = [True, False]

    async def _fake_agent(request: RunRequest):
        action_log.append(("agent", request.name))
        if request.name == "Plan Agent":
            return _plan_output(github_svc.get_open_issues.return_value)
        if request.name.startswith("Implement Agent #"):
            implemented_issue_numbers.append(int(request.name.split("#")[1]))
            return CompletionOutput()
        if request.name == "Failure Report Agent":
            return IssueOutput(number=222, labels=["bug"])
        return CompletionOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(max_parallel=4, max_iterations=1, auto_push=True),
        ),
        preflight_cache=_SequentialCache(),  # type: ignore[arg-type]
    )

    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentFailure)
    assert result.failed_role == "preflight_issue"
    assert result.issue_number == 222
    assert call_count == 2
    assert implemented_issue_numbers == [1, 2]
    assert ("agent", "Failure Report Agent") in action_log
    closed_issue_numbers = [
        call.args[0] for call in github_svc.close_issue.call_args_list
    ]
    assert closed_issue_numbers == [1]


# ── improve_max slot consumption on abort ────────────────────────────────────


def test_usage_limit_abort_does_not_consume_improve_slot(tmp_path, git_svc, logger):
    """When improve_phase raises UsageLimitError, improve_dispatched_count must not
    be incremented — the slot is only consumed by returned (completed) outcomes."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    async def _fake_agent(request: RunRequest):
        raise UsageLimitError(reset_time=None)

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(improve_max=1),
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert deps.improve_dispatched_count == 0, (
        "UsageLimitError abort must not consume an improve_max slot"
    )


def test_timeout_abort_does_not_consume_improve_slot(tmp_path, git_svc, logger):
    """When improve_phase raises AgentTimeoutError, improve_dispatched_count must not
    be incremented."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    async def _fake_agent(request: RunRequest):
        raise AgentTimeoutError("improve")

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(improve_max=1),
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedTimeout)
    assert deps.improve_dispatched_count == 0, (
        "AgentTimeoutError abort must not consume an improve_max slot"
    )


def test_no_candidate_outcome_consumes_improve_slot(tmp_path, git_svc, logger):
    """When improve_phase returns ImproveNoCandidate, improve_dispatched_count is
    incremented by 1 — NO-CANDIDATE is a returned outcome, not a raised abort."""
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        agent_responses=[NoCandidateOutput(), CompletionOutput()],
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, NoCandidate)
    assert deps.improve_dispatched_count == 1, (
        "ImproveNoCandidate must consume one improve_max slot"
    )


def test_improve_max_cap_counts_only_returned_outcomes_not_raised_aborts(
    tmp_path, git_svc, logger
):
    """improve_max=1: a UsageLimitError abort on the first attempt must not trigger the
    cap — the next call must still dispatch improve (cap fires only after 1 *returned*
    outcome)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    call_count = 0

    async def _fake_agent(request: RunRequest):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise UsageLimitError(reset_time=None)
        return NoCandidateOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(improve_max=1),
            preflight_responses=[[], []],
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    # First run: UsageLimitError → AbortedUsageLimit, count stays 0
    result1 = asyncio.run(run_iteration(deps))
    assert isinstance(result1, AbortedUsageLimit)
    assert deps.improve_dispatched_count == 0

    # Second run: NoCandidateOutput → NoCandidate, count increments to 1
    result2 = asyncio.run(run_iteration(deps))
    assert isinstance(result2, NoCandidate)
    assert deps.improve_dispatched_count == 1, (
        "improve_max cap must fire only after returned outcomes, not raised aborts"
    )


# ── TransientAgentError: iteration boundary continues without sleeping ────────


def test_run_iteration_returns_continue_on_transient_agent_error_from_implement_agent(
    tmp_path, git_svc, github_svc, logger
):
    """TransientAgentError from an Implement Agent is re-raised from implement_phase and
    caught by the top-level run_iteration boundary, returning Continue."""

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "labels": ["behavior-slice"]}]
            )
        raise TransientAgentError(status_code=529)

    deps = _make_deps(
        tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


def test_run_iteration_returns_continue_on_transient_agent_error_from_plan_agent(
    tmp_path, git_svc, logger
):
    """TransientAgentError from the Plan Agent (single-agent phase) propagates to the
    top-level run_iteration boundary and returns Continue."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_all_open_issues_lightweight.return_value = []
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            raise TransientAgentError(status_code=503)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


# ── HardAgentError: iteration boundary returns AbortedHardApiError ────────────


def test_run_iteration_returns_aborted_hard_api_error_on_hard_agent_error_from_implement_agent(
    tmp_path, git_svc, github_svc, logger
):
    """HardAgentError from an Implement Agent causes run_iteration to return AbortedHardApiError."""
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 400, "result": "Bad request: invalid model"}'

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=400)

    with patch("pycastle.iteration.auto_file_issue"):
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)


def test_run_iteration_calls_auto_file_issue_with_correct_title_and_labels_on_hard_agent_error(
    tmp_path, git_svc, github_svc, logger
):
    """HardAgentError defaults to a Claude-scoped auto-file title."""
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 401, "result": "Unauthorized: invalid token"}'

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=401)

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        mock_file.return_value = "https://github.com/x/y/issues/99"
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    mock_file.assert_called_once()
    title, body, labels = mock_file.call_args[0]
    assert title == "[pycastle] Claude API 401: Unauthorized: invalid token"
    assert result.status_code == 401
    assert labels == ["bug", "needs-triage"]
    assert raw_line in body
    assert "Service: claude" in body


def test_run_iteration_files_exact_codex_refresh_token_reused_failure_on_consuming_project(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized: '
        '{\\"type\\":\\"error\\",\\"code\\":\\"refresh_token_reused\\",'
        '\\"message\\":\\"This refresh token has already been used.\\"}"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=401, service_name="codex")

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
    owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert owner_repo == "owner/consuming-project"
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert raw_line in body
    assert "Run `codex login` on the host to reseed credentials." in body
    assert labels == ["bug", "needs-triage"]


def test_run_iteration_returns_distinct_terminal_result_for_shared_credential_failure(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized: '
        '{\\"type\\":\\"error\\",\\"code\\":\\"refresh_token_reused\\",'
        '\\"message\\":\\"This refresh token has already been used.\\"}"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=401, service_name="codex")

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    mock_file.assert_not_called()
    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    assert not isinstance(result, AbortedHardApiError)


def test_run_iteration_routes_claude_subscription_access_denial_to_shared_credential_issue(
    tmp_path, git_svc, github_svc, logger
):
    denial = (
        "Your organization has disabled Claude subscription access for Claude Code. "
        "Please use an Anthropic API key instead, or ask your admin to enable "
        "Claude subscription access for Claude Code."
    )
    raw_line = (
        '{"type": "result", "is_error": true, "api_error_status": 403, '
        f'"result": "{denial}"}}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Claude access", "labels": ["behavior-slice"]}]
            )
        process_stream([raw_line], on_turn=lambda t: None, role=AgentRole.IMPLEMENTER)
        raise AssertionError("unreachable")

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert not isinstance(result, AbortedUsageLimit)
    assert result.status_code == 403
    mock_file.assert_not_called()
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
    owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert owner_repo == "owner/consuming-project"
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert "Restore Claude Code subscription access" in body
    assert "Service: claude" in body
    assert denial in body
    assert labels == ["bug", "needs-triage"]


def test_run_iteration_prints_credential_failure_and_remediation_when_consuming_project_issue_lookup_fails(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized: '
        '{\\"type\\":\\"error\\",\\"code\\":\\"refresh_token_reused\\",'
        '\\"message\\":\\"This refresh token has already been used.\\"}"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.side_effect = RuntimeError("gh unavailable")

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=401, service_name="codex")

    display = RecordingStatusDisplay()
    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            status_display=display,
        )
        result = asyncio.run(run_iteration(deps))

    mock_file.assert_not_called()
    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    print_calls = [c for c in display.calls if c[0] == "print"]
    assert any(
        "Run `codex login` on the host to reseed credentials." in str(call[2])
        and "refresh_token_reused" in str(call[2])
        for call in print_calls
    )


def test_run_iteration_files_shared_credential_issue_for_codex_refresh_token_already_used_observation_bundle(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        err = HardAgentError(
            message=raw_line,
            status_code=401,
            service_name="codex",
            observations=(
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text=(
                        "The access token could not be refreshed because the refresh "
                        "token was already used."
                    ),
                    source_stream="stderr",
                    status_code=401,
                ),
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text=(
                        'Error: API request failed: 401 Unauthorized: {"type":"error"}'
                    ),
                    source_stream="json_event.error",
                    status_code=401,
                ),
            ),
        )
        err.caller = "Implementer"
        raise err

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
    owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert owner_repo == "owner/consuming-project"
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert labels == ["bug", "needs-triage"]
    assert "Repair local agent credentials/account access and rerun pycastle." in body
    assert "Run `codex login`" in body
    assert (
        "The access token could not be refreshed because the refresh token was already used."
        in body
    )
    assert 'Error: API request failed: 401 Unauthorized: {"type":"error"}' in body


def test_run_iteration_files_shared_credential_issue_for_refresh_token_reused_marker_observation_bundle(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        err = HardAgentError(
            message=raw_line,
            status_code=401,
            service_name="codex",
            observations=(
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text='{"code":"refresh_token_reused"}',
                    source_stream="stderr",
                    status_code=401,
                    provider_code="refresh_token_reused",
                ),
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text=(
                        'Error: API request failed: 401 Unauthorized: {"type":"error"}'
                    ),
                    source_stream="json_event.error",
                    status_code=401,
                ),
            ),
        )
        err.caller = "Implementer"
        raise err

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
    owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert owner_repo == "owner/consuming-project"
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert labels == ["bug", "needs-triage"]
    assert "Run `codex login` on the host to reseed credentials." in body
    assert '{"code":"refresh_token_reused"}' in body
    assert 'Error: API request failed: 401 Unauthorized: {"type":"error"}' in body


def test_run_iteration_builds_redacted_codex_credential_issue_body_with_bundle_context(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized: '
        '{\\"type\\":\\"error\\",\\"code\\":\\"refresh_token_reused\\",'
        '\\"message\\":\\"This refresh token has already been used for sk-live-abc123SECRET\\"}"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        err = HardAgentError(
            message=raw_line,
            status_code=401,
            service_name="codex",
            observations=(
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text=(
                        "The access token sk-live-abc123SECRET could not be refreshed "
                        "because the refresh token was already used."
                    ),
                    source_stream="stderr",
                    status_code=401,
                ),
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text=(
                        "Error: API request failed: 401 Unauthorized: "
                        '{"type":"error","code":"refresh_token_reused","message":'
                        '"This refresh token has already been used for '
                        'sk-live-abc123SECRET"}'
                    ),
                    source_stream="json_event.error",
                    status_code=401,
                ),
            ),
        )
        err.caller = "Implementer"
        raise err

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.create_issue_in.assert_called_once()
    _owner_repo, _title, body, labels = github_svc.create_issue_in.call_args[0]
    assert labels == ["bug", "needs-triage"]
    assert body.startswith(
        "Repair local agent credentials/account access and rerun pycastle."
    )
    assert (
        "This issue is about local agent-provider credentials/account access, "
        "not a source-code defect in the consuming project." in body
    )
    assert "Service: codex" in body
    assert "Agent: " in body
    assert "Agent: <unknown>" not in body
    assert "Run `codex login` on the host to reseed credentials." in body
    assert "refresh token was already used" in body
    assert "refresh_token_reused" in body
    assert "sk-live-abc123SECRET" not in body
    assert "[REDACTED]" in body


def test_run_iteration_preserves_codex_consuming_project_routing_for_distinct_credential_failure(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized: '
        '{\\"type\\":\\"error\\",\\"code\\":\\"refresh_token_reused\\",'
        '\\"message\\":\\"This refresh token has already been used.\\"}"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=raw_line,
            status_code=401,
            service_name="codex",
            classification="codex_auth_lineage_exhausted",
            observations=(
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text=raw_line,
                    source_stream="json_event.error",
                    status_code=401,
                    provider_code="refresh_token_reused",
                ),
            ),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
    owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert owner_repo == "owner/consuming-project"
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert raw_line in body
    assert "Run `codex login` on the host to reseed credentials." in body
    assert labels == ["bug", "needs-triage"]


def test_run_iteration_builds_claude_subscription_access_remediation_in_shared_credential_issue(
    tmp_path, git_svc, github_svc, logger
):
    message = (
        "Your organization has disabled Claude subscription access for Claude Code. "
        "Please ask your admin to enable Claude subscription access for Claude Code."
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=message,
            status_code=403,
            service_name="claude",
            classification="operator_actionable_agent_credential_failure",
            observations=(
                ProviderErrorObservation(
                    service_name="claude",
                    raw_provider_text=message,
                    source_stream="result",
                    status_code=403,
                ),
            ),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 403
    mock_file.assert_not_called()
    github_svc.create_issue_in.assert_called_once()
    _owner_repo, _title, body, labels = github_svc.create_issue_in.call_args[0]
    assert labels == ["bug", "needs-triage"]
    assert (
        "Restore Claude Code subscription access or use a token/account with access "
        "and rerun pycastle." in body
    )
    assert "disabled Claude subscription access for Claude Code" in body


def test_run_iteration_reuses_existing_consuming_project_issue_for_exact_codex_refresh_token_reused_failure(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 401 Unauthorized: '
        '{\\"type\\":\\"error\\",\\"code\\":\\"refresh_token_reused\\",'
        '\\"message\\":\\"This refresh token has already been used.\\"}"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = [77]

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(
            message=raw_line,
            status_code=401,
            service_name="codex",
            classification="codex_auth_lineage_exhausted",
        )

    display = RecordingStatusDisplay()
    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            status_display=display,
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.create_issue_in.assert_not_called()
    print_calls = [c for c in display.calls if c[0] == "print"]
    assert any(
        "https://github.com/owner/consuming-project/issues/77" in str(c[2])
        for c in print_calls
    )


def test_run_iteration_builds_opencode_invalid_api_key_remediation_in_shared_credential_issue(
    tmp_path, git_svc, github_svc, logger
):
    message = "OpenCode request failed: 401 invalid API key for provider opencode-go"
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=message,
            status_code=401,
            service_name="opencode",
            classification="operator_actionable_agent_credential_failure",
            observations=(
                ProviderErrorObservation(
                    service_name="opencode",
                    raw_provider_text=message,
                    source_stream="json_event.error",
                    status_code=401,
                ),
            ),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.create_issue_in.assert_called_once()
    _owner_repo, _title, body, labels = github_svc.create_issue_in.call_args[0]
    assert labels == ["bug", "needs-triage"]
    assert "Update the configured OpenCode API key and rerun pycastle." in body
    assert "invalid API key" in body


def test_run_iteration_redacts_json_credential_fields_in_shared_credential_issue_body(
    tmp_path, git_svc, github_svc, logger
):
    message = (
        '{"type":"error","code":"invalid_api_key","apiKey":"plain-secret-123456",'
        '"refreshToken":"rt-secret-123456","accessToken":"at-secret-123456"}'
    )
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=message,
            status_code=401,
            service_name="opencode",
            classification="operator_actionable_agent_credential_failure",
            observations=(
                ProviderErrorObservation(
                    service_name="opencode",
                    raw_provider_text=message,
                    source_stream="json_event.error",
                    status_code=401,
                ),
            ),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.create_issue_in.assert_called_once()
    _owner_repo, _title, body, labels = github_svc.create_issue_in.call_args[0]
    assert labels == ["bug", "needs-triage"]
    assert "plain-secret-123456" not in body
    assert "rt-secret-123456" not in body
    assert "at-secret-123456" not in body
    assert body.count("[REDACTED]") >= 3


def test_run_iteration_files_shared_credential_issue_for_missing_codex_host_auth_before_dispatch(
    tmp_path, git_svc, github_svc, logger
):
    message = "Codex authentication missing: run `codex login` on the host."
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=message,
            status_code=401,
            service_name="codex",
            observations=(
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text=message,
                    source_stream="pre-dispatch host check",
                    status_code=401,
                ),
            ),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
    _owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert labels == ["bug", "needs-triage"]
    assert (
        "Run `codex login` on the host to seed Codex credentials before dispatch."
        in body
    )
    assert "pre-dispatch host check" in body
    assert message in body


def test_run_iteration_files_shared_credential_issue_for_opencode_invalid_api_key(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = "invalid api key"
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=raw_line,
            status_code=401,
            service_name="opencode",
            classification="operator_actionable_agent_credential_failure",
            observations=(
                ProviderErrorObservation(
                    service_name="opencode",
                    raw_provider_text="invalid api key",
                    source_stream="json_event.error",
                    status_code=401,
                    error_name="AuthenticationError",
                ),
            ),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    mock_file.assert_not_called()
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
    owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert owner_repo == "owner/consuming-project"
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert labels == ["bug", "needs-triage"]
    assert "Update the configured OpenCode API key" in body
    assert "json_event.error" in body
    assert "invalid api key" in body


def test_run_iteration_does_not_route_unrelated_codex_auth_failure_to_consuming_project_issue(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 401, "result": "Unauthorized: invalid token"}'

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=401, service_name="codex")

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        mock_file.return_value = "https://github.com/Johannes-Kutsch/pycastle/issues/99"
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    mock_file.assert_called_once()
    github_svc.create_issue_in.assert_not_called()


def test_run_iteration_keeps_generic_codex_shared_classification_on_hard_provider_failure_path(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 401, "result": "Unauthorized: invalid token"}'

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=raw_line,
            status_code=401,
            service_name="codex",
            classification="operator_actionable_agent_credential_failure",
            observations=(
                ProviderErrorObservation(
                    service_name="codex",
                    raw_provider_text="Unauthorized: invalid token",
                    source_stream="json_event.error",
                    status_code=401,
                ),
            ),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        mock_file.return_value = "https://github.com/Johannes-Kutsch/pycastle/issues/99"
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    mock_file.assert_called_once()
    github_svc.search_open_issues_by_title.assert_not_called()
    github_svc.create_issue_in.assert_not_called()


def test_run_iteration_keeps_generic_codex_agent_credential_failure_without_classification_on_hard_provider_failure_path(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 401, "result": "Unauthorized: invalid token"}'

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise AgentCredentialFailureError(
            message=raw_line,
            status_code=401,
            service_name="codex",
            observations=(),
        )

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        mock_file.return_value = "https://github.com/Johannes-Kutsch/pycastle/issues/99"
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    mock_file.assert_called_once()
    github_svc.search_open_issues_by_title.assert_not_called()
    github_svc.create_issue_in.assert_not_called()


def test_run_iteration_keeps_legacy_raw_title_for_unrelated_top_level_message_hard_error(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type":"error","message":"Error: API request failed: 400 Bad Request: '
        'model_not_found"}'
    )

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Model fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=400, service_name="codex")

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        mock_file.return_value = "https://github.com/Johannes-Kutsch/pycastle/issues/99"
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    mock_file.assert_called_once()
    title, body, _labels = mock_file.call_args[0]
    assert title == f"[pycastle] Codex API 400: {raw_line}"
    assert raw_line in body
    github_svc.create_issue_in.assert_not_called()


def test_run_iteration_uses_service_name_in_hard_agent_error_title(
    tmp_path, git_svc, github_svc, logger
):
    raw_line = (
        '{"type": "error", "error": {"data": {"message": '
        '"Model not found: deepseek-v4-flash/."}}}'
    )

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Model fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=400, service_name="opencode")

    with patch("pycastle.iteration.auto_file_issue") as mock_file:
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    title, body, labels = mock_file.call_args[0]
    assert title == (
        "[pycastle] OpenCode API 400: Model not found: deepseek-v4-flash/."
    )
    assert "Service: opencode" in body
    assert labels == ["bug", "needs-triage"]


def test_run_iteration_returns_aborted_hard_api_error_on_hard_agent_error_from_plan_agent(
    tmp_path, git_svc, github_svc, logger
):
    """HardAgentError from the Plan Agent propagates to run_iteration returning AbortedHardApiError."""
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 403, "result": "Permission denied"}'

    async def agent_fn(req: RunRequest):
        raise HardAgentError(message=raw_line, status_code=403)

    # two issues so plan agent is NOT skipped
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    with patch("pycastle.iteration.auto_file_issue"):
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    assert result.status_code == 403


def test_run_iteration_emits_status_display_print_with_url_on_hard_agent_error(
    tmp_path, git_svc, github_svc, logger
):
    """On HardAgentError, run_iteration emits a status_display.print message that includes the URL."""
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 404, "result": "Not found"}'
    issue_url = "https://github.com/Johannes-Kutsch/pycastle/issues/42"

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=404)

    display = RecordingStatusDisplay()
    with patch("pycastle.iteration.auto_file_issue", return_value=issue_url):
        deps = _make_deps(
            tmp_path,
            agent_fn,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            status_display=display,
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    print_calls = [c for c in display.calls if c[0] == "print"]
    hard_error_prints = [c for c in print_calls if "hard API error" in str(c[2])]
    assert hard_error_prints, "Expected a status_display.print with 'hard API error'"
    caller, msg = hard_error_prints[-1][1], str(hard_error_prints[-1][2])
    assert "Implement Agent" in caller, (
        f"Expected agent name in caller, got: {caller!r}"
    )
    assert "404" in msg
    assert issue_url in msg


def test_run_iteration_uses_prefilled_url_when_auto_file_bugs_is_false(
    tmp_path, git_svc, github_svc, logger
):
    """When auto_file_bugs=False, run_iteration emits the prefilled issues/new URL (no API call)."""
    raw_line = '{"type": "result", "is_error": true, "api_error_status": 413, "result": "Request too large"}'

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 1, "title": "Fix", "labels": ["behavior-slice"]}]
            )
        raise HardAgentError(message=raw_line, status_code=413)

    display = RecordingStatusDisplay()
    cfg = Config(max_parallel=4, max_iterations=1, auto_file_bugs=False)
    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=display,
        cfg=cfg,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)
    assert result.status_code == 413
    print_calls = [c for c in display.calls if c[0] == "print"]
    hard_error_prints = [c for c in print_calls if "hard API error" in str(c[2])]
    assert hard_error_prints, "Expected a status_display.print with 'hard API error'"
    msg = str(hard_error_prints[-1][2])
    assert "github.com" in msg


# ── AbortedOperatorActionable: OperatorActionableGitError ────────────────────


def test_run_iteration_returns_aborted_operator_actionable_on_operator_actionable_git_error(
    tmp_path, git_svc, github_svc, logger
):
    """When OperatorActionableGitError escapes from a git operation, run_iteration
    returns AbortedOperatorActionable carrying op name, stderr snippet, and attempt count."""
    from pycastle.services import OperatorActionableGitError
    from pycastle.iteration import AbortedOperatorActionable

    err = OperatorActionableGitError(
        "git pull failed after 4 attempts",
        stderr="ssh: connect to host github.com port 22: Connection timed out",
        op="pull",
        attempt_count=4,
    )
    git_svc.pull_with_merge_fallback.side_effect = err

    async def _noop_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _noop_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedOperatorActionable)
    assert result.op == "pull"
    assert result.attempt_count == 4
    assert "timed out" in result.stderr


def test_run_iteration_operator_actionable_does_not_call_auto_file_issue_or_failure_analysis(
    tmp_path, git_svc, github_svc, logger
):
    """OperatorActionableGitError catch arm must not invoke auto_file_issue
    and must not spawn the Failure-Report agent."""
    from pycastle.services import OperatorActionableGitError
    from pycastle.iteration import AbortedOperatorActionable

    err = OperatorActionableGitError(
        "git pull failed",
        stderr="remote: Repository not found",
        op="pull",
        attempt_count=1,
    )
    git_svc.pull_with_merge_fallback.side_effect = err

    auto_file_calls: list = []

    def _recording_auto_file(title, body, labels, *, cfg):
        auto_file_calls.append((title, body))
        return ""

    async def _noop_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _noop_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with patch("pycastle.iteration.auto_file_issue", side_effect=_recording_auto_file):
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedOperatorActionable)
    assert auto_file_calls == [], (
        "auto_file_issue must not be called for OperatorActionableGitError"
    )
    agent_calls = deps.agent_runner.calls
    assert not any("Failure" in r.name for r in agent_calls), (
        "Failure-Report agent must not be spawned"
    )


# ── Issue 886: drop per-iteration cap; run all planned issues ─────────────────


def test_run_iteration_all_planned_issues_complete_when_plan_exceeds_max_parallel(
    tmp_path, git_svc, logger
):
    """With max_parallel=5 and 7 planned issues, all 7 issues complete in one iteration."""
    issues = [
        {
            "number": i,
            "title": f"Issue {i}",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
        for i in range(1, 8)
    ]
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = issues
    github_svc.get_all_open_issues_lightweight.return_value = []

    async def _agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(issues)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(max_parallel=5, max_iterations=1),
    )
    asyncio.run(run_iteration(deps))

    # try_merge is called once per completed branch in merge_phase
    assert git_svc.try_merge.call_count == 7, (
        f"Expected 7 merges (one per issue), got {git_svc.try_merge.call_count}"
    )


def test_run_iteration_status_denominator_is_planner_output_not_max_parallel(
    tmp_path, git_svc, logger
):
    """Status row denominator Y in 'started implement Agents for X/Y' equals the planner output count, not max_parallel."""
    issues = [
        {
            "number": i,
            "title": f"Issue {i}",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
        for i in range(1, 8)
    ]
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = issues
    github_svc.get_all_open_issues_lightweight.return_value = []

    async def _agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(issues)
        return CompletionOutput()

    sd = RecordingStatusDisplay()
    deps = _make_deps(
        tmp_path,
        _agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(max_parallel=5, max_iterations=1),
        status_display=sd,
    )
    asyncio.run(run_iteration(deps))

    update_phase_bodies = [
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    ]
    assert any("0/7" in b for b in update_phase_bodies), (
        f"Expected initial '0/7' in status bodies, got: {update_phase_bodies}"
    )
    assert any("7/7" in b for b in update_phase_bodies), (
        f"Expected terminal '7/7' in status bodies, got: {update_phase_bodies}"
    )
    assert not any("/5" in b for b in update_phase_bodies), (
        f"Denominator must be 7 (planner output), not 5 (max_parallel); got: {update_phase_bodies}"
    )


def test_run_iteration_max_parallel_1_all_issues_in_one_iteration(
    tmp_path, git_svc, logger
):
    """With max_parallel=1 and multiple planned issues, all run in one iteration and one merge phase closes it."""
    issues = [
        {
            "number": i,
            "title": f"Issue {i}",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
        for i in range(1, 4)
    ]
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = issues
    github_svc.get_all_open_issues_lightweight.return_value = []

    async def _agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output(issues)
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        cfg=Config(max_parallel=1, max_iterations=1),
    )
    asyncio.run(run_iteration(deps))

    # All 3 issues merged in the single merge phase
    assert git_svc.try_merge.call_count == 3, (
        f"Expected 3 merges (all issues), got {git_svc.try_merge.call_count}"
    )


def test_merge_phase_reports_merging_counter_for_all_clean_branches(
    tmp_path, git_svc, logger
):
    completed = [
        {"number": 1, "title": "Fix A"},
        {"number": 2, "title": "Fix B"},
    ]
    github_svc = MagicMock(spec=GithubService)
    status_display = RecordingStatusDisplay()

    git_svc.try_merge.side_effect = [True, True]

    deps = _make_deps(
        tmp_path,
        FakeAgentRunner(),
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=status_display,
    )

    asyncio.run(merge_phase(completed, deps))

    merge_updates = [
        call[2]
        for call in status_display.calls
        if call[0] == "update_phase" and call[1] == "Merge"
    ]
    assert "merging 2/2 branches" in merge_updates


def test_merge_phase_reports_closing_counter_without_replacing_merging(
    tmp_path, git_svc, logger
):
    completed = [
        {"number": 1, "title": "Fix A"},
        {"number": 2, "title": "Fix B"},
    ]
    github_svc = MagicMock(spec=GithubService)
    status_display = RecordingStatusDisplay()

    git_svc.try_merge.side_effect = [True, True]

    deps = _make_deps(
        tmp_path,
        FakeAgentRunner(),
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=status_display,
    )

    asyncio.run(merge_phase(completed, deps))

    merge_updates = [
        call[2]
        for call in status_display.calls
        if call[0] == "update_phase" and call[1] == "Merge"
    ]
    assert "merging 2/2 branches, closing 1/2 issues" in merge_updates
    assert "merging 2/2 branches, closing 2/2 issues" in merge_updates


def test_merge_phase_only_shows_removing_counter_during_active_deletion(
    tmp_path, git_svc, logger
):
    completed = [
        {"number": 1, "title": "Clean fix"},
        {"number": 2, "title": "Conflict fix"},
    ]
    github_svc = MagicMock(spec=GithubService)
    status_display = RecordingStatusDisplay()

    git_svc.try_merge.side_effect = [True, False]
    git_svc.get_current_branch.return_value = "main"

    async def _agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path,
        _agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=status_display,
    )

    asyncio.run(merge_phase(completed, deps))

    merge_updates = [
        call[2]
        for call in status_display.calls
        if call[0] == "update_phase" and call[1] == "Merge"
    ]
    assert "merging 1/2 branches, closing 1/2 issues, removing 1/2 worktrees" in (
        merge_updates
    )
    assert "merging 2/2 branches, closing 2/2 issues" in merge_updates
