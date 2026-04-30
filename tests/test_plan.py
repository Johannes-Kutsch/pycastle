import asyncio
import json

import pytest
from unittest.mock import MagicMock

from pycastle.agent_result import AgentIncomplete, AgentSuccess
from pycastle.config import Config
from pycastle.errors import PreflightError
from pycastle.git_service import GitService
from pycastle.github_service import GithubService
from pycastle.iteration._deps import Deps, RecordingLogger
from pycastle.iteration.plan import (
    PlanAFK,
    PlanHITL,
    PlanReady,
    _handle_preflight_failure,
    plan_phase,
    strip_stale_blocker_refs,
)


def _plan_json(issues: list[dict]) -> str:
    return f"<plan>{json.dumps({'issues': issues})}</plan>"


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


def _make_deps(tmp_path, run_agent_fn, *, git_svc, github_svc, logger):
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        run_agent=run_agent_fn,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger,
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

    async def run_agent(name, **kwargs):
        return AgentIncomplete(partial_output=_plan_json(expected))

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanReady)
    assert result.issues == expected
    assert result.worktree_sha == "abc123"


def test_plan_phase_returns_empty_ready_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    planner_calls: list[str] = []

    async def run_agent(name, **kwargs):
        planner_calls.append(name)
        return AgentIncomplete(partial_output="")

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanReady)
    assert result.issues == []
    assert planner_calls == [], f"Planner must not be called; got {planner_calls}"


def test_plan_phase_passes_stale_blocker_refs_stripped_to_planner(
    tmp_path, git_svc, logger
):
    open_issues = [
        {"number": 10, "title": "Issue", "body": "Blocked by #99\nReal content"}
    ]
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = open_issues
    captured: dict = {}

    async def run_agent(name, prompt_args=None, **kwargs):
        captured["prompt_args"] = prompt_args or {}
        return AgentIncomplete(partial_output='<plan>{"issues": []}</plan>')

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    received = json.loads(captured["prompt_args"]["OPEN_ISSUES_JSON"])
    assert received[0]["body"] == "Real content"


def test_plan_phase_returns_ready_when_planner_returns_agent_success(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 3, "title": "Another fix"}]
    github_svc.get_open_issues.return_value = expected

    async def run_agent(name, **kwargs):
        return AgentSuccess(output=_plan_json(expected))

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanReady)
    assert result.issues == expected


# ── plan_phase: PlanParseError ────────────────────────────────────────────────


def test_plan_phase_raises_runtime_error_when_no_plan_tag(
    tmp_path, git_svc, github_svc, logger
):
    async def run_agent(name, **kwargs):
        return AgentIncomplete(partial_output="no plan tag in this output")

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(RuntimeError, match="no <plan> tag"):
        asyncio.run(plan_phase(deps))


# ── plan_phase: HITL routing ──────────────────────────────────────────────────


def test_plan_phase_returns_hitl_on_hitl_preflight_verdict(tmp_path, git_svc, logger):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-human"]

    async def run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(
            partial_output='<issue label="ready-for-human">55</issue>'
        )

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanHITL)
    assert result.issue_number == 55
    assert result.worktree_sha == "abc123"


def test_plan_phase_returns_hitl_when_preflight_agent_returns_agent_success(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-human"]

    async def run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentSuccess(output='<issue label="ready-for-human">99</issue>')

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(plan_phase(deps))

    assert isinstance(result, PlanHITL)
    assert result.issue_number == 99


# ── plan_phase: AFK routing ───────────────────────────────────────────────────


def test_plan_phase_returns_afk_on_afk_preflight_verdict(tmp_path, git_svc, logger):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-agent"]
    github_svc.get_issue_title.return_value = "Fix preflight issue"

    async def run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(
            partial_output='<issue label="ready-for-agent">42</issue>'
        )

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
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

    async def run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(partial_output="no issue tag here")

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(RuntimeError, match="issue"):
        asyncio.run(plan_phase(deps))


# ── _handle_preflight_failure: mount_path forwarding ─────────────────────────


def test_handle_preflight_failure_forwards_mount_path_to_run_agent(
    tmp_path, git_svc, github_svc, logger
):
    custom_mount = tmp_path / "pre-planning"
    custom_mount.mkdir()
    assert custom_mount != tmp_path  # mount_path differs from repo_root

    captured: dict = {}

    async def run_agent(name, mount_path=None, **kwargs):
        captured[name] = mount_path
        return AgentIncomplete(partial_output='<issue label="ready-for-agent">7</issue>')

    github_svc.get_labels.return_value = ["ready-for-agent"]

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(
        _handle_preflight_failure([("ruff", "ruff check .", "E501")], deps, custom_mount)
    )

    agent_name = next(k for k in captured if "preflight-issue" in k)
    assert captured[agent_name] == custom_mount
    assert captured[agent_name] != tmp_path


# ── plan_phase: pre-planning worktree ────────────────────────────────────────


def test_plan_phase_calls_checkout_detached_with_head_sha(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_open_issues.return_value = expected

    async def run_agent(name, **kwargs):
        return AgentIncomplete(partial_output=_plan_json(expected))

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-planning"
    git_svc.checkout_detached.assert_called_once_with(tmp_path, expected_worktree, "abc123")


def test_plan_phase_passes_worktree_path_as_mount_path_to_planner(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_open_issues.return_value = expected
    captured: dict = {}

    async def run_agent(name, mount_path=None, **kwargs):
        captured[name] = mount_path
        return AgentIncomplete(partial_output=_plan_json(expected))

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-planning"
    assert captured["Planner"] == expected_worktree
    assert captured["Planner"] != tmp_path


def test_plan_phase_removes_worktree_after_successful_planning(
    tmp_path, git_svc, github_svc, logger
):
    expected = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_open_issues.return_value = expected

    async def run_agent(name, **kwargs):
        return AgentIncomplete(partial_output=_plan_json(expected))

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-planning"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_plan_phase_removes_worktree_when_preflight_fails(tmp_path, git_svc, logger):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-agent"]
    github_svc.get_issue_title.return_value = "Preflight issue"

    async def run_agent(name, **kwargs):
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(partial_output='<issue label="ready-for-agent">42</issue>')

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-planning"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_plan_phase_removes_worktree_when_planner_raises(tmp_path, git_svc, github_svc, logger):
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]

    async def run_agent(name, **kwargs):
        raise RuntimeError("unexpected crash")

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError, match="unexpected crash"):
        asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-planning"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_plan_phase_passes_worktree_path_to_preflight_issue_agent(tmp_path, git_svc, logger):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-agent"]
    github_svc.get_issue_title.return_value = "Preflight issue"
    captured: dict = {}

    async def run_agent(name, mount_path=None, **kwargs):
        captured[name] = mount_path
        if name == "Planner":
            raise PreflightError([("ruff", "ruff check .", "E501")])
        return AgentIncomplete(partial_output='<issue label="ready-for-agent">42</issue>')

    deps = _make_deps(
        tmp_path, run_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(plan_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-planning"
    preflight_agent = next(k for k in captured if "preflight-issue" in k)
    assert captured[preflight_agent] == expected_worktree
    assert captured[preflight_agent] != tmp_path


# ── _handle_preflight_failure: mount_path forwarding ─────────────────────────
