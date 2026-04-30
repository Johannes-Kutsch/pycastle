import asyncio
from unittest.mock import MagicMock

import pytest

from pycastle.agent_result import (
    AgentSuccess,
    AgentTimeoutHit,
    PreflightFailure,
    UsageLimitHit,
)
from pycastle.config import Config, config as _cfg
from pycastle.errors import AgentTimeoutError
from pycastle.git_service import GitService
from pycastle.github_service import GithubService
from pycastle.iteration._deps import Deps, RecordingLogger
from pycastle.iteration.implement import (
    ImplementResult,
    branch_for,
    implement_phase,
    run_issue,
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


def test_implement_phase_logs_implementer_output_on_success(tmp_path):
    """Implementer output is passed to deps.logger.log_agent_output() when it succeeds."""
    issues = [{"number": 7, "title": "Fix C"}]
    logger = RecordingLogger()
    agent_output = "<promise>COMPLETE</promise>"

    async def _fake_run_agent(name, **kwargs):
        return AgentSuccess(output=agent_output)

    deps = _make_deps(tmp_path, _fake_run_agent, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert len(logger.agent_outputs) == 1
    assert logger.agent_outputs[0] == ("Implementer #7", agent_output)


def test_implement_phase_reviewer_usage_limit_signals_in_result(tmp_path):
    """When reviewer hits usage limit, implement_phase returns usage_limit_hit=True and issue is not completed."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return UsageLimitHit(last_output="")

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.usage_limit_hit is True
    assert result.completed == []
    assert result.errors == []


# ── run_issue: prompt args and skip_preflight ─────────────────────────────────


def test_run_issue_derives_branch_from_issue_number(tmp_path):
    """run_issue must derive the branch via branch_for(number) and include it in prompt_args."""
    captured: dict = {}

    async def _fake_run_agent(name, prompt_args=None, branch=None, **kw):
        if "Implementer" in name:
            captured["branch_kwarg"] = branch
            captured["branch_prompt_arg"] = (prompt_args or {}).get("BRANCH")
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 7, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)
    asyncio.run(run_issue(issue, deps))

    assert captured["branch_kwarg"] == "pycastle/issue-7"
    assert captured["branch_prompt_arg"] == "pycastle/issue-7"


def test_run_issue_passes_feedback_commands_to_implementer(tmp_path):
    """run_issue must include FEEDBACK_COMMANDS in prompt_args for the implementer."""
    captured_args: list[dict] = []

    async def _fake_run_agent(name, prompt_args=None, **kw):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    assert "FEEDBACK_COMMANDS" in implementer_call["prompt_args"]


def test_run_issue_feedback_commands_include_backtick_wrapped_implement_checks(
    tmp_path,
):
    """FEEDBACK_COMMANDS must be formatted from IMPLEMENT_CHECKS with backtick wrapping."""
    captured_args: list[dict] = []

    async def _fake_run_agent(name, prompt_args=None, **kw):
        captured_args.append({"name": name, "prompt_args": prompt_args or {}})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(a for a in captured_args if "Implementer" in a["name"])
    feedback_commands = implementer_call["prompt_args"]["FEEDBACK_COMMANDS"]
    for cmd in _cfg.implement_checks:
        assert f"`{cmd}`" in feedback_commands


def test_run_issue_implementer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the implementer agent."""
    captured: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        captured.append({"name": name, "skip_preflight": skip_preflight})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)
    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in captured if "Implementer" in c["name"])
    assert impl_call["skip_preflight"] is True


def test_run_issue_reviewer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the reviewer agent."""
    captured: list[dict] = []

    async def _fake_run_agent(name, skip_preflight=False, **kwargs):
        captured.append({"name": name, "skip_preflight": skip_preflight})
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)
    asyncio.run(run_issue(issue, deps))

    rev_call = next(c for c in captured if "Reviewer" in c["name"])
    assert rev_call["skip_preflight"] is True


def test_run_issue_returns_none_when_implementer_does_not_complete(tmp_path):
    """run_issue must return None when implementer response lacks COMPLETE tag."""

    async def _fake_run_agent(**kwargs):
        return "I tried but could not finish"

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(run_issue(issue, deps))

    assert result is None


def test_run_issue_returns_issue_when_implementer_completes(tmp_path):
    """run_issue must return the issue dict when implementer produces COMPLETE."""

    async def _fake_run_agent(**kwargs):
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 2, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue


# ── Cycle 274: AgentTimeoutHit propagation through implement layer ────────────


def test_run_issue_raises_agent_timeout_error_when_implementer_exhausts_retries(
    tmp_path,
):
    """When implementer returns AgentTimeoutHit, run_issue must raise AgentTimeoutError."""

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentTimeoutHit(last_output="partial work")
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    issue = {"number": 5, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run_issue(issue, deps))


def test_run_issue_raises_agent_timeout_error_when_reviewer_exhausts_retries(tmp_path):
    """When reviewer returns AgentTimeoutHit, run_issue must raise AgentTimeoutError."""

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentTimeoutHit(last_output="")

    issue = {"number": 5, "title": "Fix thing"}
    deps = _make_deps(tmp_path, _fake_run_agent)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run_issue(issue, deps))


def test_implement_phase_implementer_timeout_tracked_as_error(tmp_path):
    """When implementer exhausts retries, implement_phase tracks the issue in errors."""
    issues = [{"number": 3, "title": "Fix C"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentTimeoutHit(last_output="")
        return AgentSuccess(output="<promise>COMPLETE</promise>")

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[0]
    assert isinstance(result.errors[0][1], AgentTimeoutError)


def test_implement_phase_reviewer_timeout_does_not_complete_issue(tmp_path):
    """When reviewer exhausts retries, the issue must not appear in completed."""
    issues = [{"number": 4, "title": "Fix D"}]

    async def _fake_run_agent(name, **kwargs):
        if "Implementer" in name:
            return AgentSuccess(output="<promise>COMPLETE</promise>")
        return AgentTimeoutHit(last_output="")

    deps = _make_deps(tmp_path, _fake_run_agent)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert len(result.errors) == 1
    assert isinstance(result.errors[0][1], AgentTimeoutError)
