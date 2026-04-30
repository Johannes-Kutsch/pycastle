import asyncio
import json
from unittest.mock import MagicMock

import pytest

from pycastle.agent_result import (
    AgentIncomplete,
    AgentSuccess,
    PreflightFailure,
    UsageLimitHit,
)
from pycastle.config import Config
from pycastle.git_service import GitService
from pycastle.github_service import GithubService
from pycastle.iteration import (
    AbortedHITL,
    AbortedUsageLimit,
    Continue,
    Done,
    run_iteration,
)
from pycastle.iteration._deps import Deps, RecordingLogger


def _plan_json(issues: list[dict]) -> str:
    return f"<plan>{json.dumps({'issues': issues})}</plan>"


@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    svc.try_merge.return_value = True
    svc.is_ancestor.return_value = True
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
) -> Deps:
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        run_agent=run_agent_fn,
        cfg=cfg or Config(max_parallel=4, max_iterations=1),
        logger=logger,
    )


# ── Done: no open issues ──────────────────────────────────────────────────────


def test_run_iteration_returns_done_when_no_open_issues(tmp_path, git_svc, logger):
    """run_iteration returns Done when plan_phase finds no open issues."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []

    async def _noop_agent(name, **kwargs):
        return AgentIncomplete(partial_output="")

    deps = _make_deps(
        tmp_path, _noop_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Done)


# ── AbortedHITL: HITL preflight verdict ──────────────────────────────────────


def test_run_iteration_returns_aborted_hitl_on_hitl_verdict(tmp_path, git_svc, logger):
    """run_iteration returns AbortedHITL when plan_phase returns PlanHITL."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-human"]

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
        return AgentIncomplete(
            partial_output='<issue label="ready-for-human">42</issue>'
        )

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedHITL)
    assert result.issue_number == 42


def test_run_iteration_aborted_hitl_carries_issue_number(tmp_path, git_svc, logger):
    """AbortedHITL must carry the issue number filed by the preflight-issue agent."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-human"]

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return PreflightFailure(
                failures=(("mypy", "mypy .", "error: Missing module"),)
            )
        return AgentIncomplete(
            partial_output='<issue label="ready-for-human">99</issue>'
        )

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
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
    github_svc.get_labels.return_value = ["ready-for-human"]

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
        return AgentIncomplete(
            partial_output='<issue label="ready-for-human">7</issue>'
        )

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    result = asyncio.run(run_iteration(deps))
    assert isinstance(result, AbortedHITL)


# ── AbortedUsageLimit: usage limit hit ───────────────────────────────────────


def test_run_iteration_raises_when_planner_hits_usage_limit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration raises RuntimeError when the Planner returns UsageLimitHit (no plan tag)."""

    async def _fake_agent(name, **kwargs):
        return UsageLimitHit(last_output="token ceiling reached")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError):
        asyncio.run(run_iteration(deps))


def test_run_iteration_returns_aborted_usage_limit_when_implementer_hits_limit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns AbortedUsageLimit when an implementer hits the usage limit."""

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        return UsageLimitHit(last_output="")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, AbortedUsageLimit)


def test_run_iteration_aborted_usage_limit_does_not_raise_system_exit(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration must return AbortedUsageLimit instead of calling sys.exit on usage limit."""

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        return UsageLimitHit(last_output="")

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

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


def test_run_iteration_returns_continue_when_no_implementers_complete(
    tmp_path, git_svc, github_svc, logger
):
    """run_iteration returns Continue (not Done) when implementers produce no commits."""

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return AgentIncomplete(
                partial_output=_plan_json([{"number": 1, "title": "Fix"}])
            )
        return AgentIncomplete(partial_output="")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


# ── PlanAFK: preflight failure with AFK verdict ───────────────────────────────


def test_run_iteration_returns_continue_on_afk_preflight_verdict(
    tmp_path, git_svc, logger
):
    """run_iteration implements the preflight-fix issue and returns Continue when
    plan_phase returns PlanAFK (preflight failure with AFK verdict)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-agent"]
    github_svc.get_issue_title.return_value = "Preflight fix"

    async def _fake_agent(name, **kwargs):
        if name == "Planner":
            return PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
        if "preflight-issue" in name:
            return AgentIncomplete(
                partial_output='<issue label="ready-for-agent">55</issue>'
            )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(run_iteration(deps))

    assert isinstance(result, Continue)


def test_run_iteration_afk_path_spawns_implementer_for_fix_issue(
    tmp_path, git_svc, logger
):
    """On AFK preflight verdict, run_iteration must spawn an Implementer for the
    filed fix issue (not invoke the Planner a second time)."""
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_labels.return_value = ["ready-for-agent"]
    github_svc.get_issue_title.return_value = "Preflight fix"

    agent_names: list[str] = []

    async def _fake_agent(name, **kwargs):
        agent_names.append(name)
        if name == "Planner":
            return PreflightFailure(failures=(("mypy", "mypy .", "error"),))
        if "preflight-issue" in name:
            return AgentIncomplete(
                partial_output='<issue label="ready-for-agent">77</issue>'
            )
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentIncomplete(partial_output="")

    deps = _make_deps(
        tmp_path, _fake_agent, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(run_iteration(deps))

    planner_calls = [n for n in agent_names if n == "Planner"]
    implementer_calls = [n for n in agent_names if "Implementer" in n]
    assert len(planner_calls) == 1, "Planner called once (returns PreflightFailure)"
    assert len(implementer_calls) == 1, "Exactly one Implementer for the fix issue"
    assert implementer_calls[0] == "Implementer #77"
