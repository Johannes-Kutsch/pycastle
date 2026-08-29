import asyncio
import dataclasses
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from agent_runtime.errors import (
    HardAgentError,
)

from pycastle.agent_credential_failure_routing import (
    AgentCredentialFailureRouteResult,
)
from pycastle.agents.output_protocol import (
    AgentRole,
    CompletionOutput,
    IssueOutput,
    NoCandidateOutput,
    PlannerOutput,
    PromiseParseError,
)
from pycastle.agents.runner import RunRequest
from pycastle.config import Config
from pycastle.errors import (
    AgentFailedError,
    AgentTimeoutError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.iteration import (
    AbortedAgentCredentialFailure,
    AbortedAgentFailure,
    AbortedHardApiError,
    AbortedHITL,
    AbortedModelNotAvailable,
    AbortedSetup,
    AbortedTimeout,
    AbortedUsageLimit,
    Continue,
    Done,
    NoCandidate,
    run_iteration,
)
from pycastle.iteration._deps import (
    Deps,
)
from pycastle.iteration.merge import merge_phase
from pycastle.iteration.planning import planning_phase
from pycastle.iteration.preflight import PreflightCache, PreflightReady
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import GithubService, GitService
from pycastle.session import RoleSession
from tests.support import (
    FETCH_CONNECTION_TIMEOUT,
    FETCH_REPO_NOT_FOUND,
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
    StubPreflightCache,
    functional_git_svc,
    make_scan_output,
)
from tests.support import (
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
    svc.get_branch_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    svc.try_merge.return_value = True
    svc.is_ancestor.return_value = True
    svc.verify_ref_exists.return_value = False
    svc.start_merge.return_value = False
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
    **_kw,
) -> Deps:
    cfg = _kw.get("cfg")
    status_display = _kw.get("status_display")
    preflight_responses = _kw.get("preflight_responses")
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
    github_svc.repo = "test/repo"
    github_svc.create_issue_in.return_value = (0, 0)

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output([filed_issue])
        if request.name == "Scan Agent":
            return make_scan_output()
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            draft_dir.mkdir(parents=True, exist_ok=True)
            body = "A" * 120
            (draft_dir / "spec.md").write_text(
                f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n---\n\n{body}"
            )
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


def test_single_ready_issue_planning_uses_carried_ready_slice_outcome_for_work_body_and_template(
    tmp_path, git_svc, github_svc, logger
):
    from pycastle.issue_readiness import (
        IssueReadiness,
        IssueReadinessKind,
        ReadyIssueOutcome,
        SliceMode,
        WellFormed,
        WellFormedBody,
    )

    issue_title = "Fix auth bug"
    carried_readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.REFACTOR, label="refactor-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=None,
        ready=ReadyIssueOutcome(
            display_name="docs",
            template=PromptTemplate.IMPLEMENT_DOCS,
        ),
        kind=IssueReadinessKind.READY_AFK,
    )
    github_svc.get_open_issues.return_value = [
        {
            "number": 3,
            "title": issue_title,
            "body": "x" * 100,
            "labels": ["behavior-slice"],
            "readiness": carried_readiness,
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
    reviewer_calls = [c for c in recording_runner.calls if "Review Agent" in c.name]
    assert len(implementer_calls) == 1
    assert implementer_calls[0].prompt.template == PromptTemplate.IMPLEMENT_DOCS
    assert implementer_calls[0].work_body == f'implementing docs "{issue_title}"'
    assert len(reviewer_calls) == 1
    assert reviewer_calls[0].work_body == f'reviewing docs "{issue_title}"'


@pytest.mark.parametrize(
    ("label", "mode"),
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

    def _selector(candidates, *, repo_root, git_svc, operating_branch="main"):
        del repo_root, git_svc, operating_branch
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

    def _selector(candidates, *, repo_root, git_svc, operating_branch="main"):
        del repo_root, git_svc, operating_branch
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
# every combination of improve_mode x slept_once x improve-phase outcome.


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
    github_svc.repo = "test/repo"
    github_svc.create_issue_in.return_value = (0, 0)

    response_queue = list(agent_responses)

    async def _agent(request: RunRequest):
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            draft_dir.mkdir(parents=True, exist_ok=True)
            body = "A" * 120
            (draft_dir / "spec.md").write_text(
                f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n---\n\n{body}"
            )
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
        agent_responses=[make_scan_output(), CompletionOutput(), CompletionOutput()],
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


def test_run_iteration_until_sleep_resumes_interrupted_cycle_when_slept(
    tmp_path, git_svc, logger
):
    """until_sleep + slept_once=True + improve_cycle_interrupted=True + 0 AFK → improve dispatched.

    An interrupted improve cycle must be resumed even after sleep; the cycle
    completes and improve_cycle_interrupted is cleared to False afterward.
    """
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="until_sleep",
        slept_once=True,
        agent_responses=[make_scan_output(), CompletionOutput(), CompletionOutput()],
    )
    deps.improve_cycle_interrupted = True

    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue), (
        "An interrupted improve cycle must be resumed and the iteration must continue"
    )
    assert not deps.improve_cycle_interrupted, (
        "improve_cycle_interrupted must be cleared once the cycle completes"
    )


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
        agent_responses=[make_scan_output(), CompletionOutput(), CompletionOutput()],
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
        agent_responses=[make_scan_output(), CompletionOutput(), CompletionOutput()],
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
        agent_responses=[make_scan_output(), CompletionOutput(), CompletionOutput()],
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
        agent_responses=[make_scan_output(), CompletionOutput(), CompletionOutput()],
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
    git_svc.get_branch_sha.return_value = "safe-sha-from-preflight"
    deps = _make_improve_deps(
        tmp_path,
        git_svc,
        logger,
        improve_mode="endless",
        slept_once=False,
        agent_responses=[make_scan_output(), CompletionOutput(), CompletionOutput()],
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


def test_usage_limit_in_improve_leaves_dispatched_count_unchanged(
    tmp_path, git_svc, logger
):
    """improve_dispatched_count must NOT increment when improve_phase raises UsageLimitError.

    Only ImproveContinue (PRD + sub-issues filed) counts as a dispatched cycle.
    A usage-limit abort preserves the worktree on disk; iteration 2 resumes the
    Scan Agent where it left off, so the slot must not be consumed.
    """
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
            cfg=Config(improve_max=1),
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)
    assert deps.improve_dispatched_count == 0, (
        "improve_dispatched_count must remain 0 after a usage-limit abort — "
        "only ImproveContinue (filed improvements) consumes a slot"
    )


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
        (path / "sentinel.txt").write_text("")

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
        (session.path / "_continuation").write_text(
            "opaque-token",
            encoding="utf-8",
        )
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
    git_svc.try_merge.side_effect = [True, False, True]

    async def _fake_agent(request: RunRequest):
        action_log.append(("agent", request.name))
        if request.name == "Plan Agent":
            return _plan_output(github_svc.get_open_issues.return_value)
        if request.name.startswith("Implement Agent #"):
            implemented_issue_numbers.append(int(request.name.split("#")[1]))
        return CompletionOutput()

    def _push(repo_root: Path, branch: str, resolver):
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
        call.args[0] for call in github_svc.close_issue_with_parents.call_args_list
    ]
    assert closed_issue_numbers == [1, 181]
    deleted_branches = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-1" in deleted_branches
    assert "pycastle/issue-181" in deleted_branches
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
    github_svc.close_issue_with_parents.side_effect = lambda issue_number: (
        action_log.append(("close_issue", issue_number))
    )

    git_svc.delete_branch.side_effect = lambda branch, repo_root: action_log.append(
        ("delete_branch", branch)
    )
    git_svc.push.side_effect = lambda repo_root, branch, resolver: action_log.append(
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
        call.args[0] for call in github_svc.close_issue_with_parents.call_args_list
    ]
    assert closed_issue_numbers == [1]
    deleted_branches = [call.args[0] for call in git_svc.delete_branch.call_args_list]
    assert "pycastle/issue-1" in deleted_branches
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
            (preflight_path / "sentinel.txt").write_text("")
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
    github_svc.close_issue_with_parents.side_effect = lambda issue_number: (
        action_log.append(("close_issue", issue_number))
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
        call.args[0] for call in github_svc.close_issue_with_parents.call_args_list
    ]
    assert closed_issue_numbers == [1]


# ── improve_max slot consumption on abort ────────────────────────────────────


def test_timeout_abort_leaves_improve_dispatched_count_unchanged(
    tmp_path, git_svc, logger
):
    """When improve_phase raises AgentTimeoutError, improve_dispatched_count must NOT
    increment — only ImproveContinue (PRD + sub-issues filed) consumes a slot.

    A timeout preserves the worktree on disk; iteration 2 resumes the Scan Agent
    where it left off, so the slot must not be consumed prematurely.
    """
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
        "AgentTimeoutError must not consume an improve slot — "
        "only ImproveContinue (filed improvements) increments the counter"
    )


def test_no_candidate_outcome_leaves_improve_dispatched_count_unchanged(
    tmp_path, git_svc, logger
):
    """When improve_phase returns ImproveNoCandidate, improve_dispatched_count must NOT
    increment — only ImproveContinue (PRD + sub-issues filed) consumes a slot."""
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
    assert deps.improve_dispatched_count == 0, (
        "ImproveNoCandidate must not consume an improve slot — "
        "only ImproveContinue (filed improvements) increments the counter"
    )


def test_improve_continue_increments_dispatched_count_by_one(tmp_path, git_svc, logger):
    """When improve_phase returns ImproveContinue (PRD + sub-issues filed),
    improve_dispatched_count increments by exactly 1."""
    filed_issue = {
        "number": 5,
        "title": "Improve: refactor X",
        "body": "x" * 100,
        "comments": [],
        "labels": ["refactor-slice"],
    }
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.side_effect = [[], [filed_issue]]
    github_svc.get_all_open_issues_lightweight.return_value = []
    github_svc.repo = "test/repo"
    github_svc.create_issue_in.return_value = (0, 0)

    async def _fake_agent(request: RunRequest):
        if request.name == "Plan Agent":
            return _plan_output([filed_issue])
        if request.name == "Scan Agent":
            return make_scan_output()
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            draft_dir.mkdir(parents=True, exist_ok=True)
            body = "A" * 120
            (draft_dir / "spec.md").write_text(
                f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n---\n\n{body}"
            )
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
        improve_dispatched_count=0,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert deps.improve_dispatched_count == 1, (
        "ImproveContinue must increment improve_dispatched_count by exactly 1"
    )


def test_preflight_hitl_in_improve_leaves_dispatched_count_unchanged(
    tmp_path, git_svc, logger
):
    """When improve_phase returns PreflightHITL (preflight gate blocked with human-intervention
    label), improve_dispatched_count must NOT increment."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    async def _fake_agent(request: RunRequest):
        return IssueOutput(number=42, labels=["ready-for-human"])

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(improve_max=1),
            preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHITL)
    assert deps.improve_dispatched_count == 0, (
        "PreflightHITL must not consume an improve slot — "
        "only ImproveContinue (filed improvements) increments the counter"
    )


def test_preflight_afk_in_improve_leaves_dispatched_count_unchanged(
    tmp_path, git_svc, logger
):
    """When improve_phase returns PreflightAFK (preflight gate blocked with AFK label),
    improve_dispatched_count must NOT increment."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
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

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(improve_max=1),
            preflight_responses=[[_preflight_failure("ruff", "ruff check .", "E501")]],
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)
    assert deps.improve_dispatched_count == 0, (
        "PreflightAFK must not consume an improve slot — "
        "only ImproveContinue (filed improvements) increments the counter"
    )


def test_improve_max_cap_not_consumed_by_usage_limit_abort(tmp_path, git_svc, logger):
    """improve_max=1: a UsageLimitError on the first attempt does NOT consume the slot,
    so the second iteration dispatches improve_phase again (not Done).

    This is the correct cycle-based invariant: only ImproveContinue (filed improvements)
    consumes a slot. A usage-limit abort preserves the worktree on disk; iteration 2
    resumes the Scan Agent where it left off.
    """
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
    call_count = 0

    async def _fake_agent(request: RunRequest):
        nonlocal call_count
        call_count += 1
        raise UsageLimitError(reset_time=None)

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
    # First iteration: UsageLimitError → AbortedUsageLimit, count stays 0
    result1 = asyncio.run(run_iteration(deps))
    assert isinstance(result1, AbortedUsageLimit)
    assert deps.improve_dispatched_count == 0

    # Second iteration: cap not reached (0 < improve_max=1) → improve dispatched again
    result2 = asyncio.run(run_iteration(deps))
    assert isinstance(result2, AbortedUsageLimit), (
        "improve_phase must be dispatched again on iteration 2 since the slot was not consumed"
    )
    assert deps.improve_dispatched_count == 0


def _seed_improve_progress(worktree_path: Path, phase_id: str) -> None:
    import json as _json

    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    if phase_id == "01-scan:picked":
        # Simulate scan done with one candidate, cursor at 0 (no record → PRD phase)
        data = {"candidates": [{"rank": 1, "title": "Seeded candidate"}]}
        (role_session_dir / "_candidate_list").write_text(
            _json.dumps(data), encoding="utf-8"
        )
        (role_session_dir / "_candidate_cursor").write_text("0", encoding="utf-8")
    (role_session_dir / "_fingerprint").write_text("abc123", encoding="utf-8")


def _improve_restart_git_svc():
    # No-ops remove_worktree/list_worktrees so REUSABLE_SANDBOX cleanup doesn't delete pre-seeded session files.
    svc = functional_git_svc()
    svc.get_head_sha.return_value = "abc123"
    svc.get_branch_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    svc.list_worktrees.side_effect = None
    svc.list_worktrees.return_value = []
    svc.remove_worktree.side_effect = None
    return svc


def test_phase1_restart_leaves_improve_dispatched_count_unchanged(tmp_path, logger):
    """When improve_phase restarts from phase 1 due to missing transcript handoff,
    improve_dispatched_count must NOT increment — no improvement was completed."""
    git_svc = _improve_restart_git_svc()
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_improve_progress(wt, "01-scan:picked")

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
    github_svc.get_all_open_issues_lightweight.return_value = []

    # setup_worktrees=False so that _wire_worktrees is not called again, which
    # would overwrite the no-op remove_worktree we need to preserve session files.
    deps = dataclasses.replace(
        _make_test_deps(
            tmp_path,
            FakeAgentRunner([], preflight_responses=[[]]),
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_cache=StubPreflightCache(),
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    asyncio.run(run_iteration(deps))

    assert deps.improve_dispatched_count == 0, (
        "A phase-1 restart must not consume an improve slot — "
        "only a completed improvement (PRD + sub-issues filed) increments the counter"
    )


def test_improve_cap_not_consumed_by_phase1_restart(tmp_path, logger):
    """With improve_max=1, a phase-1 restart does not consume the improve cap.
    A second run_iteration call dispatches improve again (not Done)."""
    git_svc = _improve_restart_git_svc()
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_improve_progress(wt, "01-scan:picked")

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
    github_svc.get_all_open_issues_lightweight.return_value = []

    runner = FakeAgentRunner(
        # First run: restart — no agents called.
        # Second run: fresh phase-1 scan → no-candidate → report (2 agents).
        [NoCandidateOutput(), CompletionOutput()],
        preflight_responses=[[], []],
    )
    # setup_worktrees=False so that _wire_worktrees is not called again.
    deps = dataclasses.replace(
        _make_test_deps(
            tmp_path,
            runner,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            cfg=Config(improve_max=1),
            preflight_cache=StubPreflightCache(),
        ),
        improve_mode="endless",
        improve_dispatched_count=0,
    )
    # First run: phase-1 restart, cap slot not consumed
    asyncio.run(run_iteration(deps))
    assert deps.improve_dispatched_count == 0

    # Second run: cap not reached (0 < 1), improve is dispatched again
    result2 = asyncio.run(run_iteration(deps))
    assert not isinstance(result2, Done), (
        "After a phase-1 restart the cap slot must remain available; "
        "a second run must dispatch improve, not return Done(improve_cap_reached=True)"
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
        raise TransientAgentError

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
            raise TransientAgentError
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
        raise HardAgentError(message=raw_line)

    with patch("pycastle.iteration.auto_file_issue"):
        deps = _make_deps(
            tmp_path, agent_fn, git_svc=git_svc, github_svc=github_svc, logger=logger
        )
        result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHardApiError)


def test_run_iteration_returns_distinct_terminal_result_for_shared_credential_failure(
    tmp_path, git_svc, github_svc, logger
):
    hard_error = HardAgentError(
        message="Credential failure surfaced by the provider adapter.",
        service_name="codex",
    )
    hard_error.caller = "Implementer"

    async def agent_fn(req: RunRequest):
        if req.name == "Plan Agent":
            return _plan_output(
                [{"number": 2, "title": "Auth fix", "labels": ["behavior-slice"]}]
            )
        raise hard_error

    display = RecordingStatusDisplay()
    deps = _make_deps(
        tmp_path,
        agent_fn,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
        status_display=display,
    )

    with (
        patch("pycastle.iteration.auto_file_issue") as mock_file,
        patch(
            "pycastle.iteration.route_agent_credential_failure",
            return_value=AgentCredentialFailureRouteResult(
                status_code=401,
                status_message="operator-actionable agent credential failure: status 401",
                issue_url="https://github.com/owner/consuming-project/issues/42",
            ),
        ) as mock_route,
    ):
        result = asyncio.run(run_iteration(deps))

    mock_file.assert_not_called()
    mock_route.assert_called_once_with(
        provider_failure=hard_error,
        github_svc=github_svc,
    )
    assert isinstance(result, AbortedAgentCredentialFailure)
    assert result.status_code == 401
    assert not isinstance(result, AbortedHardApiError)
    assert any(
        call[0] == "print"
        and "operator-actionable agent credential failure: status 401" in str(call[2])
        and "https://github.com/owner/consuming-project/issues/42" in str(call[2])
        for call in display.calls
    )


# ── AbortedOperatorActionable: OperatorActionableGitError ────────────────────


def test_run_iteration_returns_aborted_operator_actionable_on_operator_actionable_git_error(
    tmp_path, git_svc, github_svc, logger
):
    """When OperatorActionableGitError escapes from a git operation, run_iteration
    returns AbortedOperatorActionable carrying op name, stderr snippet, and attempt count."""
    from pycastle.iteration import AbortedOperatorActionable

    git_svc.refresh_operating_branch.side_effect = FETCH_CONNECTION_TIMEOUT

    async def _noop_agent(request: RunRequest):
        return CompletionOutput()

    deps = _make_deps(
        tmp_path, _noop_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedOperatorActionable)
    assert result.op == "fetch"
    assert result.attempt_count == 4
    assert "timed out" in result.stderr


def test_run_iteration_operator_actionable_does_not_call_auto_file_issue_or_failure_analysis(
    tmp_path, git_svc, github_svc, logger
):
    """OperatorActionableGitError catch arm must not invoke auto_file_issue
    and must not spawn the Failure-Report agent."""
    from pycastle.iteration import AbortedOperatorActionable

    git_svc.refresh_operating_branch.side_effect = FETCH_REPO_NOT_FOUND

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


# ── AbortedModelNotAvailable: model not available ────────────────────────────


def test_run_iteration_returns_aborted_model_not_available_when_model_not_available(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns AbortedModelNotAvailable when the agent raises
    ModelNotAvailableError, so the orchestrator can route through the continuation
    decision instead of crashing."""
    from pycastle.errors import ModelNotAvailableError

    async def _fake_agent(request: RunRequest):
        raise ModelNotAvailableError(service="claude", model="claude-opus-4-5")

    deps = _make_deps(
        tmp_path,
        _fake_agent,
        git_svc=git_svc,
        github_svc=github_svc,
        logger=logger,
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedModelNotAvailable)
    assert result.service == "claude"
    assert result.model == "claude-opus-4-5"


# ── Planner fingerprint gate ──────────────────────────────────────────────────


def _two_planning_issues() -> list[dict]:
    return [
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


def _plan_output_for(issues: list[dict]) -> PlannerOutput:
    return PlannerOutput(
        issues=[{"number": i["number"], "title": i["title"]} for i in issues]
    )


def test_planning_phase_fingerprint_gate_discards_session_on_sha_change(tmp_path):
    """When the safe SHA changes, the fingerprint gate discards the prior Planner session
    and the new fingerprint is written to the refreshed session."""
    issues = _two_planning_issues()
    plan_wt = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    session = RoleSession(plan_wt, AgentRole.PLANNER)
    session.write_fingerprint("stale-fingerprint-wont-match")
    session.write_continuation("saved-context")

    fake = FakeAgentRunner([_plan_output_for(issues[:1])])
    deps = _make_test_deps(
        tmp_path,
        fake,
        preflight_cache=StubPreflightCache(PreflightReady(sha="sha-v2")),
    )
    asyncio.run(planning_phase(deps, issues, issues))

    assert not session.is_resumable()
    assert session.read_fingerprint() is not None


def test_planning_phase_fingerprint_gate_preserves_session_on_match(tmp_path):
    """When the safe SHA and open issue set are unchanged, the prior Planner
    session is preserved so an interrupted run can resume."""
    issues = _two_planning_issues()

    # git_svc where remove_worktree is a no-op so session files survive worktree
    # lifecycle calls (models the real git behavior where the branch persists).
    git_svc = MagicMock(spec=GitService)
    git_svc.is_working_tree_clean.return_value = True
    git_svc.verify_ref_exists.return_value = False
    git_svc.list_worktrees.return_value = []
    plan_wt = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"

    def _fake_create_wt(repo, wt, branch, sha=None):
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")
        (wt / "sentinel.txt").write_text("")

    git_svc.create_worktree.side_effect = _fake_create_wt

    preflight_cache = StubPreflightCache(PreflightReady(sha="sha-v1"))

    fake1 = FakeAgentRunner([_plan_output_for(issues[:1])])
    deps1 = _make_test_deps(
        tmp_path, fake1, git_svc=git_svc, preflight_cache=preflight_cache
    )
    asyncio.run(planning_phase(deps1, issues, issues))

    session = RoleSession(plan_wt, AgentRole.PLANNER)
    session.write_continuation("interrupted-context")

    fake2 = FakeAgentRunner([_plan_output_for(issues[:1])])
    deps2 = _make_test_deps(
        tmp_path, fake2, git_svc=git_svc, preflight_cache=preflight_cache
    )
    asyncio.run(planning_phase(deps2, issues, issues))

    assert session.is_resumable()
    assert session.read_fingerprint() is not None


# ── Startable filter (ADR 0059) ───────────────────────────────────────────────


def test_issues_with_open_blockers_are_excluded_from_planner_candidate_set(
    tmp_path, git_svc, logger
):
    """An issue with open_blockers_count > 0 must not appear in the ready-for-agent list
    the Planner receives. When all ready candidates are blocked, Done is returned and the
    Plan Agent is never dispatched."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Blocked issue A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
            "open_blockers_count": 1,
        },
        {
            "number": 2,
            "title": "Blocked issue B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
            "open_blockers_count": 2,
        },
    ]
    github_svc.get_all_open_issues_lightweight.return_value = []

    plan_agent_called = False

    async def _fake_agent(request: RunRequest):
        nonlocal plan_agent_called
        if request.name == "Plan Agent":
            plan_agent_called = True
            return _plan_output(
                [
                    {
                        "number": 1,
                        "title": "Blocked issue A",
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

    assert isinstance(result, Done), f"expected Done, got {result!r}"
    assert not plan_agent_called, (
        "Plan Agent must not be called when all candidates have open blockers"
    )


def test_fully_blocked_backlog_does_not_dispatch_improve_mode(
    tmp_path, git_svc, logger
):
    """When all ready-for-agent issues have open blockers, improve mode must not be
    dispatched even when improve_mode='endless'. The iteration ends with Done, not
    NoCandidate or Continue."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": 1,
            "title": "Blocked issue",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
            "open_blockers_count": 1,
        }
    ]
    github_svc.get_all_open_issues_lightweight.return_value = []

    improve_agent_called = False

    async def _fake_agent(request: RunRequest):
        nonlocal improve_agent_called
        if request.name == "Scan Agent":
            improve_agent_called = True
        return CompletionOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[]],
        ),
        improve_mode="endless",
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Done), f"expected Done, got {result!r}"
    assert not improve_agent_called, (
        "improve mode must not be dispatched for a blocked backlog"
    )


def test_in_flight_issue_with_open_blocker_is_still_resumed(tmp_path, git_svc, logger):
    """An issue carrying preserved work (continuation file) must be resumed even when
    its open_blockers_count > 0. The startable filter applies only to fresh candidates,
    not to already-started work."""
    from pycastle.agents.output_protocol import AgentRole as _AgentRole
    from pycastle.infrastructure.worktree import worktree_identity
    from pycastle.iteration.implement import branch_for
    from pycastle.session import SESSION_DIR_NAME

    issue_number = 1
    branch = branch_for(issue_number)
    worktree_path = worktree_identity(branch, tmp_path).path
    continuation_path = (
        worktree_path
        / SESSION_DIR_NAME
        / _AgentRole.IMPLEMENTER.value
        / "_continuation"
    )
    continuation_path.parent.mkdir(parents=True)
    continuation_path.write_text("resume-token", encoding="utf-8")

    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [
        {
            "number": issue_number,
            "title": "In-flight blocked issue",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
            "open_blockers_count": 1,
        }
    ]
    github_svc.get_all_open_issues_lightweight.return_value = []

    async def _fake_agent(request: RunRequest):
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

    assert isinstance(result, Continue), (
        "An in-flight blocked issue must be resumed, yielding Continue"
    )


def test_empty_backlog_still_dispatches_improve_mode(tmp_path, git_svc, logger):
    """When there are no ready-for-agent issues at all (truly empty), improve mode is
    still dispatched as before. The startable filter must not affect the improve gate."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
    github_svc.get_all_open_issues_lightweight.return_value = []

    improve_agent_called = False

    async def _fake_agent(request: RunRequest):
        nonlocal improve_agent_called
        if request.name == "Scan Agent":
            improve_agent_called = True
            return NoCandidateOutput()
        return CompletionOutput()

    deps = dataclasses.replace(
        _make_deps(
            tmp_path,
            _fake_agent,
            git_svc=git_svc,
            github_svc=github_svc,
            logger=logger,
            preflight_responses=[[]],
        ),
        improve_mode="endless",
    )
    asyncio.run(run_iteration(deps))

    assert improve_agent_called, (
        "improve mode must be dispatched when the backlog is truly empty"
    )
