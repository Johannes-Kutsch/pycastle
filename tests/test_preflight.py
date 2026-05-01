import asyncio
import dataclasses

import pytest
from unittest.mock import MagicMock

from pycastle.config import Config
from pycastle.git_service import GitService
from pycastle.github_service import GithubService
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    NullStatusDisplay,
    RecordingLogger,
    RecordingStatusDisplay,
)
from pycastle.iteration.plan import PlanAFK, PlanHITL
from pycastle.iteration.preflight import PreflightReady, preflight_phase


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


# ── preflight_phase: no open issues ──────────────────────────────────────────


def test_preflight_phase_returns_ready_with_empty_issues_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"
    assert result.issues == []


def test_preflight_phase_does_not_call_run_preflight_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    assert fake.preflight_calls == [], (
        f"run_preflight must not be called when no issues; got {fake.preflight_calls}"
    )


def test_preflight_phase_does_not_create_worktree_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    git_svc.checkout_detached.assert_not_called()


# ── preflight_phase: preflight passes ────────────────────────────────────────


def test_preflight_phase_returns_ready_with_open_issues_when_preflight_passes(
    tmp_path, git_svc, github_svc, logger
):
    issues = [{"number": 1, "title": "Fix bug", "body": ""}]
    github_svc.get_open_issues.return_value = issues
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"
    assert result.issues == issues


def test_preflight_phase_calls_checkout_detached_with_head_sha(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.checkout_detached.assert_called_once_with(
        tmp_path, expected_worktree, "abc123"
    )


def test_preflight_phase_passes_worktree_path_as_mount_path_to_run_preflight(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    assert len(fake.preflight_calls) == 1
    assert fake.preflight_calls[0]["mount_path"] == expected_worktree


# ── preflight_phase: HITL routing ────────────────────────────────────────────


def test_preflight_phase_returns_hitl_on_hitl_preflight_verdict(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ['<issue>{"number": 55, "labels": ["bug", "ready-for-human"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PlanHITL)
    assert result.issue_number == 55
    assert result.worktree_sha == "abc123"


# ── preflight_phase: AFK routing ─────────────────────────────────────────────


def test_preflight_phase_returns_afk_on_afk_preflight_verdict(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Fix preflight issue"
    fake = FakeAgentRunner(
        ['<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PlanAFK)
    assert result.issues == [{"number": 42, "title": "Fix preflight issue"}]
    assert result.worktree_sha == "abc123"


def test_preflight_phase_hitl_routing_uses_configured_hitl_label(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ['<issue>{"number": 55, "labels": ["custom-bug", "custom-human"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(
        deps,
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            bug_label="custom-bug",
            issue_label="custom-agent",
            hitl_label="custom-human",
        ),
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PlanHITL)
    assert result.issue_number == 55


# ── preflight_phase: IssueParseError → RuntimeError ─────────────────────────


def test_preflight_phase_raises_runtime_error_when_preflight_agent_returns_no_issue_tag(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ["<promise>COMPLETE</promise>no issue tag here"],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(RuntimeError, match="issue"):
        asyncio.run(preflight_phase(deps))


# ── preflight_phase: worktree lifecycle ──────────────────────────────────────


def test_preflight_phase_removes_worktree_after_passing_preflight(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_preflight_phase_removes_worktree_when_preflight_fails(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Preflight issue"
    fake = FakeAgentRunner(
        ['<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_preflight_phase_removes_worktree_when_exception_raised(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[RuntimeError("container error")])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError, match="container error"):
        asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


# ── preflight_phase: status_display threading ────────────────────────────────


def test_preflight_phase_passes_status_display_to_run_preflight(
    tmp_path, git_svc, github_svc, logger
):
    recording = RecordingStatusDisplay()
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(deps, status_display=recording)

    asyncio.run(preflight_phase(deps))

    assert len(fake.preflight_calls) == 1
    assert fake.preflight_calls[0]["status_display"] is recording


def test_preflight_phase_uses_preflight_checks_as_run_preflight_name(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    asyncio.run(preflight_phase(deps))

    assert len(fake.preflight_calls) == 1
    assert fake.preflight_calls[0]["name"] == "preflight-checks"
