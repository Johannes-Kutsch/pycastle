import asyncio
from unittest.mock import MagicMock

import pytest

from pycastle.agent_result import AgentSuccess, PreflightFailure, UsageLimitHit
from pycastle.config import Config
from pycastle.git_service import GitService
from pycastle.github_service import GithubService
from pycastle.iteration._deps import Deps, RecordingLogger
from pycastle.iteration.implement import ImplementResult, branch_for, implement_phase


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def deps(tmp_path, logger: RecordingLogger) -> Deps:
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=MagicMock(spec=GitService),
        github_svc=MagicMock(spec=GithubService),
        run_agent=None,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger,
    )


def _make_deps(tmp_path, run_agent_fn, logger=None) -> Deps:
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=MagicMock(spec=GitService),
        github_svc=MagicMock(spec=GithubService),
        run_agent=run_agent_fn,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger or RecordingLogger(),
    )


# ── branch_for ────────────────────────────────────────────────────────────────


def test_branch_for_returns_pycastle_issue_format():
    assert branch_for(193) == "pycastle/issue-193"


def test_branch_for_uses_issue_number():
    assert branch_for(1) == "pycastle/issue-1"
    assert branch_for(42) == "pycastle/issue-42"


# ── implement_phase: parallel execution (tracer bullet) ───────────────────────


def test_implement_phase_returns_completed_issues(tmp_path):
    """implement_phase returns all issues in completed when every agent returns COMPLETE."""
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]

    async def _fake_run_agent(name, **kwargs):
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == issues
    assert result.errors == []
    assert result.usage_limit_hit is False


# ── implement_phase: usage-limit signalling ───────────────────────────────────


def test_implement_phase_signals_usage_limit_in_result(tmp_path):
    """implement_phase returns usage_limit_hit=True instead of calling sys.exit."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _fake_run_agent(name, **kwargs):
        return UsageLimitHit(last_output="")

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.usage_limit_hit is True


def test_implement_phase_usage_limit_does_not_exit(tmp_path):
    """implement_phase must not call sys.exit() when a usage limit is hit."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _fake_run_agent(name, **kwargs):
        return UsageLimitHit(last_output="")

    deps = _make_deps(tmp_path, _fake_run_agent)

    # Should not raise SystemExit
    result = asyncio.run(implement_phase(issues, None, deps))
    assert isinstance(result, ImplementResult)


def test_implement_phase_usage_limit_awaits_siblings(tmp_path):
    """When one issue hits usage limit, sibling tasks must complete before returning."""
    completed_agents: list[str] = []
    issues = [{"number": 1, "title": "Fail"}, {"number": 2, "title": "Pass"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer #1" in name:
            return UsageLimitHit(last_output="")
        completed_agents.append(name)
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    deps = _make_deps(tmp_path, _fake_run_agent)
    asyncio.run(implement_phase(issues, None, deps))

    assert any("Implementer #2" in n for n in completed_agents), (
        f"Sibling Implementer #2 must complete before returning; completed={completed_agents}"
    )


# ── implement_phase: per-issue error collection ───────────────────────────────


def test_implement_phase_preflight_failure_goes_to_errors(tmp_path):
    """PreflightFailure returned by run_agent lands in result.errors."""
    issues = [{"number": 1, "title": "Fix A"}]
    failure = PreflightFailure(failures=(("mypy", "mypy .", "error: missing module"),))

    async def _fake_run_agent(name, **kwargs):
        return failure

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[0]
    assert result.errors[0][1] is failure


def test_implement_phase_exception_goes_to_errors(tmp_path):
    """An exception raised by run_agent lands in result.errors."""
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer #1" in name or "Reviewer #1" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        raise RuntimeError("agent failed")

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == [issues[0]]
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[1]
    assert isinstance(result.errors[0][1], RuntimeError)


def test_implement_phase_no_complete_tag_dropped_from_both_lists(tmp_path):
    """When run_issue returns None (no COMPLETE tag), issue is absent from both lists."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _fake_run_agent(name, **kwargs):
        return "some response without the complete tag"

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert result.errors == []


# ── implement_phase: errors passed to logger ─────────────────────────────────


def test_implement_phase_logs_preflight_failure_via_logger(tmp_path):
    """PreflightFailure must be passed to deps.logger.log_error()."""
    issues = [{"number": 1, "title": "Fix A"}]
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    logger = RecordingLogger()

    async def _fake_run_agent(name, **kwargs):
        return failure

    deps = _make_deps(tmp_path, _fake_run_agent, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert len(logger.errors) == 1
    assert logger.errors[0][0] == issues[0]
    assert logger.errors[0][1] is failure


def test_implement_phase_logs_exception_via_logger(tmp_path):
    """Exceptions raised during run_issue must be passed to deps.logger.log_error()."""
    issues = [{"number": 1, "title": "Fix A"}]
    boom = RuntimeError("agent crashed")
    logger = RecordingLogger()

    async def _fake_run_agent(name, **kwargs):
        raise boom

    deps = _make_deps(tmp_path, _fake_run_agent, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert len(logger.errors) == 1
    assert logger.errors[0][0] == issues[0]
    assert logger.errors[0][1] is boom


def test_implement_phase_successful_issues_not_logged_as_errors(tmp_path):
    """Completed issues must not produce log_error() calls."""
    issues = [{"number": 1, "title": "Fix A"}]
    logger = RecordingLogger()

    async def _fake_run_agent(name, **kwargs):
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    deps = _make_deps(tmp_path, _fake_run_agent, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert logger.errors == []
