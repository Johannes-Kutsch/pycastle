import asyncio
import json

import pytest
from unittest.mock import MagicMock

from pycastle.agent_result import PreflightFailure
from pycastle.config import Config
from pycastle.services import GitService
from pycastle.services import GithubService
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    NullStatusDisplay,
    RecordingLogger,
)
from pycastle.iteration.planning import PlanReady, planning_phase


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


# ── planning_phase: returns PlanReady with sorted issues ────────────────────


def test_planning_phase_returns_plan_ready_with_issues_sorted_by_number(
    tmp_path, git_svc, github_svc, logger
):
    issues = [
        {"number": 3, "title": "C"},
        {"number": 1, "title": "A"},
        {"number": 2, "title": "B"},
    ]
    plan_output = _plan_json(
        [
            {"number": 3, "title": "C"},
            {"number": 1, "title": "A"},
            {"number": 2, "title": "B"},
        ]
    )
    fake = FakeAgentRunner([plan_output])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(planning_phase(deps, "abc123", issues))

    assert isinstance(result, PlanReady)
    assert result.worktree_sha == "abc123"
    assert [i["number"] for i in result.issues] == [1, 2, 3]


# ── planning_phase: skip_preflight ──────────────────────────────────────────


def test_planning_phase_invokes_planner_with_skip_preflight_true(
    tmp_path, git_svc, github_svc, logger
):
    issues = [{"number": 1, "title": "A"}]
    plan_output = _plan_json([{"number": 1, "title": "A"}])
    fake = FakeAgentRunner([plan_output])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(planning_phase(deps, "abc123", issues))

    assert len(fake.calls) == 1
    assert fake.calls[0]["skip_preflight"] is True


# ── planning_phase: worktree lifecycle ──────────────────────────────────────


def test_planning_phase_removes_worktree_after_success(
    tmp_path, git_svc, github_svc, logger
):
    issues = [{"number": 1, "title": "A"}]
    plan_output = _plan_json([{"number": 1, "title": "A"}])
    fake = FakeAgentRunner([plan_output])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(planning_phase(deps, "abc123", issues))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_planning_phase_removes_worktree_when_exception_raised(
    tmp_path, git_svc, github_svc, logger
):
    issues = [{"number": 1, "title": "A"}]
    fake = FakeAgentRunner([RuntimeError("agent crashed")])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError, match="agent crashed"):
        asyncio.run(planning_phase(deps, "abc123", issues))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


# ── planning_phase: error paths ─────────────────────────────────────────────


def test_planning_phase_raises_runtime_error_when_planner_returns_preflight_failure(
    tmp_path, git_svc, github_svc, logger
):
    issues = [{"number": 1, "title": "A"}]
    fake = FakeAgentRunner(
        [PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))]
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError, match="PreflightFailure unexpectedly"):
        asyncio.run(planning_phase(deps, "abc123", issues))


def test_planning_phase_raises_runtime_error_when_planner_output_has_no_plan_tag(
    tmp_path, git_svc, github_svc, logger
):
    issues = [{"number": 1, "title": "A"}]
    fake = FakeAgentRunner(["output without a plan tag"])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError, match="no <plan> tag"):
        asyncio.run(planning_phase(deps, "abc123", issues))


# ── planning_phase: edge cases ───────────────────────────────────────────────


def test_planning_phase_with_empty_issues_list_still_invokes_planner_and_returns_ready(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([_plan_json([])])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(planning_phase(deps, "abc123", []))

    assert isinstance(result, PlanReady)
    assert result.issues == []
    assert result.worktree_sha == "abc123"
    assert len(fake.calls) == 1
