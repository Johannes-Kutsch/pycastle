import asyncio
import json

import pytest
from unittest.mock import MagicMock

from pycastle.agent_result import (
    PreflightFailure,
)
from pycastle.errors import UsageLimitError
from pycastle.config import Config
from pycastle.git_service import GitService
from pycastle.github_service import GithubService
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    NullStatusDisplay,
    RecordingLogger,
)
from pycastle.iteration.plan import (
    PlanAFK,
    PlanHITL,
    PlanReady,
    plan_phase,
    strip_stale_blocker_refs,
)


def _plan_json(issues: list[dict]) -> str:
    return f"<promise>COMPLETE</promise><plan>{json.dumps({'issues': issues})}</plan>"


@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    return svc


@pytest.fixture
def github_svc():
    svc = MagicMock(spec=GithubService)
    svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    return svc


@pytest.fixture
def logger():
    return RecordingLogger()


def _make_deps(tmp_path, agent_runner, *, git_svc, github_svc, logger):
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=agent_runner,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger,
        status_display=NullStatusDisplay(),
    )


# ── strip_stale_blocker_refs ──────────────────────────────────────────────────


def test_strip_stale_blocker_refs_removes_line_referencing_closed_blocker():
    issues = [{"number": 1, "title": "A", "body": "Blocked by #99\nOther content"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == "Other content"


def test_strip_stale_blocker_refs_handles_none_body():
    issues = [{"number": 1, "title": "A", "body": None}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_preserves_line_referencing_open_blocker():
    issues = [
        {"number": 1, "title": "A", "body": "Blocked by #2\nContent"},
        {"number": 2, "title": "B", "body": ""},
    ]
    result = strip_stale_blocker_refs(issues)
    assert "Blocked by #2" in result[0]["body"]


def test_strip_stale_blocker_refs_empty_list():
    assert strip_stale_blocker_refs([]) == []


def test_strip_stale_blocker_refs_handles_missing_body_key():
    issues = [{"number": 1, "title": "A"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_preserves_other_fields():
    issues = [{"number": 7, "title": "T", "state": "open", "body": "Blocked by #99"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["number"] == 7
    assert result[0]["title"] == "T"
    assert result[0]["state"] == "open"


# ── plan_phase: success path ──────────────────────────────────────────────────


def test_plan_phase_returns_ready_with_parsed_issues(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_open_issues.return_value = expected
    fake = FakeAgentRunner([_plan_json(expected)])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanReady)
    assert result.issues == expected
    assert result.worktree_sha == "abc123"


def test_plan_phase_returns_empty_ready_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanReady)
    assert result.issues == []
    assert fake.calls == [], f"Planner must not be called; got {fake.calls}"


def test_plan_phase_passes_stale_blocker_refs_stripped_to_planner(
    tmp_path, git_svc, logger
):
    open_issues = [
        {"number": 10, "title": "Issue", "body": "Blocked by #99\nReal content"}
    ]
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = open_issues
    fake = FakeAgentRunner(['<promise>COMPLETE</promise><plan>{"issues": []}</plan>'])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    received = json.loads(fake.calls[0]["prompt_args"]["OPEN_ISSUES_JSON"])
    assert received[0]["body"] == "Real content"


def test_plan_phase_returns_ready_when_planner_returns_agent_success(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 3, "title": "Another fix"}]
    github_svc.get_open_issues.return_value = expected
    fake = FakeAgentRunner([_plan_json(expected)])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanReady)
    assert result.issues == expected


# ── plan_phase: UsageLimitError ──────────────────────────────────────────────


def test_plan_phase_removes_worktree_when_planner_hits_usage_limit(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([UsageLimitError("")])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(UsageLimitError):
        asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


# ── plan_phase: PlanParseError ────────────────────────────────────────────────


def test_plan_phase_raises_runtime_error_when_no_plan_tag(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>no plan tag in this output"])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(RuntimeError, match="no <plan> tag"):
        asyncio.run(plan_phase(deps))


# ── plan_phase: HITL routing ──────────────────────────────────────────────────


def test_plan_phase_returns_hitl_on_hitl_preflight_verdict(tmp_path, git_svc, logger):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            '<issue>{"number": 55, "labels": ["bug", "ready-for-human"]}</issue>',
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanHITL)
    assert result.issue_number == 55
    assert result.worktree_sha == "abc123"


def test_plan_phase_returns_hitl_when_preflight_agent_includes_promise_tag(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            '<issue>{"number": 99, "labels": ["bug", "ready-for-human"]}</issue>',
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanHITL)
    assert result.issue_number == 99


def test_plan_phase_hitl_verdict_from_agent_output_not_github_labels(
    tmp_path, git_svc, logger
):
    """HITL verdict comes from IssueOutput.label in agent output, not from github_svc.get_labels."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            '<issue>{"number": 33, "labels": ["bug", "ready-for-human"]}</issue>',
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanHITL)
    github_svc.get_labels.assert_not_called()


# ── plan_phase: AFK routing ───────────────────────────────────────────────────


def test_plan_phase_returns_afk_on_afk_preflight_verdict(tmp_path, git_svc, logger):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Fix preflight issue"
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            '<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>',
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanAFK)
    assert result.issues == [{"number": 42, "title": "Fix preflight issue"}]
    assert result.worktree_sha == "abc123"


# ── plan_phase: IssueParseError → RuntimeError ───────────────────────────────


def test_plan_phase_raises_runtime_error_when_preflight_agent_returns_no_issue_tag(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            "<promise>COMPLETE</promise>no issue tag here",
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(RuntimeError, match="issue"):
        asyncio.run(plan_phase(deps))


# ── plan_phase: plan-sandbox worktree ────────────────────────────────────────


def test_plan_phase_calls_checkout_detached_with_head_sha(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_open_issues.return_value = expected
    fake = FakeAgentRunner([_plan_json(expected)])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.checkout_detached.assert_called_once_with(
        tmp_path, expected_worktree, "abc123"
    )


def test_plan_phase_passes_worktree_path_as_mount_path_to_planner(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_open_issues.return_value = expected
    fake = FakeAgentRunner([_plan_json(expected)])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    assert fake.calls[0]["mount_path"] == expected_worktree
    assert fake.calls[0]["mount_path"] != tmp_path


def test_plan_phase_removes_worktree_after_successful_planning(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_open_issues.return_value = expected
    fake = FakeAgentRunner([_plan_json(expected)])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_plan_phase_removes_worktree_when_preflight_fails(tmp_path, git_svc, logger):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Preflight issue"
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            '<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>',
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_plan_phase_removes_worktree_when_planner_raises(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner([RuntimeError("unexpected crash")])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError, match="unexpected crash"):
        asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_plan_phase_passes_worktree_path_to_preflight_issue_agent(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Preflight issue"
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            '<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>',
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    # calls[1] is the preflight-issue agent call
    preflight_call = next(c for c in fake.calls if "preflight-issue" in c["name"])
    assert preflight_call["mount_path"] == expected_worktree
    assert preflight_call["mount_path"] != tmp_path


# ── plan_phase: edge cases ────────────────────────────────────────────────────


def test_plan_phase_raises_usage_limit_error_when_planner_hits_usage_limit(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([UsageLimitError("")])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(plan_phase(deps))


def test_plan_phase_preflight_failure_only_first_check_spawns_agent(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Preflight fix"
    fake = FakeAgentRunner(
        [
            PreflightFailure(
                failures=(
                    ("ruff", "ruff check .", "E501"),
                    ("mypy", "mypy .", "mypy error"),
                    ("pytest", "pytest", "test failed"),
                )
            ),
            '<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>',
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    preflight_calls = [c for c in fake.calls if "preflight-issue" in c["name"]]
    assert len(preflight_calls) == 1
    assert preflight_calls[0]["prompt_args"]["CHECK_NAME"] == "ruff"
    assert preflight_calls[0]["prompt_args"]["COMMAND"] == "ruff check ."


def test_plan_phase_raises_when_preflight_issue_agent_hits_usage_limit(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            UsageLimitError(""),
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(plan_phase(deps))


def test_plan_phase_removes_worktree_when_preflight_issue_agent_hits_usage_limit(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        [
            PreflightFailure(failures=(("ruff", "ruff check .", "E501"),)),
            UsageLimitError(""),
        ]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)
