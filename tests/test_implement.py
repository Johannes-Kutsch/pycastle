import asyncio
from unittest.mock import MagicMock

import pytest

from pycastle.agent_result import (
    PreflightFailure,
)
from pycastle.agent_runner import RunRequest
from pycastle.config import Config
from pycastle.errors import AgentTimeoutError, UsageLimitError
from pycastle.services import GitService
from pycastle.services import GithubService
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    NullStatusDisplay,
    RecordingLogger,
)
from pycastle.iteration.implement import (
    ImplementResult,
    branch_for,
    implement_phase,
    run_issue,
)

_cfg = Config()


def _make_deps(tmp_path, agent_runner, logger=None) -> Deps:
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=MagicMock(spec=GitService),
        github_svc=MagicMock(spec=GithubService),
        agent_runner=agent_runner,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger or RecordingLogger(),
        status_display=NullStatusDisplay(),
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
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 4)

    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == issues
    assert result.errors == []
    assert result.usage_limit_hit is False


def test_implement_phase_empty_issues_returns_empty_result(tmp_path):
    """implement_phase with no issues makes no agent calls and returns an empty result."""
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase([], None, deps))

    assert result.completed == []
    assert result.errors == []
    assert result.usage_limit_hit is False
    assert fake.calls == []


# ── implement_phase: usage-limit signalling ───────────────────────────────────


def test_implement_phase_signals_usage_limit_in_result(tmp_path):
    """implement_phase returns usage_limit_hit=True instead of calling sys.exit."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _side_effect(request: RunRequest):
        raise UsageLimitError("")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.usage_limit_hit is True


def test_implement_phase_usage_limit_does_not_exit(tmp_path):
    """implement_phase must not call sys.exit() when a usage limit is hit."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _side_effect(request: RunRequest):
        raise UsageLimitError("")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)

    # Should not raise SystemExit
    result = asyncio.run(implement_phase(issues, None, deps))
    assert isinstance(result, ImplementResult)


def test_implement_phase_usage_limit_awaits_siblings(tmp_path):
    """When one issue hits usage limit, sibling tasks must complete before returning."""
    completed_agents: list[str] = []

    async def _side_effect(request: RunRequest):
        if "Implementer #1" in request.name:
            raise UsageLimitError("")
        completed_agents.append(request.name)
        return "<promise>COMPLETE</promise>"

    issues = [{"number": 1, "title": "Fail"}, {"number": 2, "title": "Pass"}]
    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    asyncio.run(implement_phase(issues, None, deps))

    assert any("Implementer #2" in n for n in completed_agents), (
        f"Sibling Implementer #2 must complete before returning; completed={completed_agents}"
    )


# ── implement_phase: per-issue error collection ───────────────────────────────


def test_implement_phase_preflight_failure_goes_to_errors(tmp_path):
    """PreflightFailure returned by run_agent lands in result.errors."""
    issues = [{"number": 1, "title": "Fix A"}]
    failure = PreflightFailure(failures=(("mypy", "mypy .", "error: missing module"),))
    fake = FakeAgentRunner([failure])

    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[0]
    assert result.errors[0][1] is failure


def test_implement_phase_exception_goes_to_errors(tmp_path):
    """An exception raised by run_agent lands in result.errors."""
    issues = [{"number": 1, "title": "Fix A"}, {"number": 2, "title": "Fix B"}]

    async def _side_effect(request: RunRequest):
        if "Implementer #1" in request.name or "Reviewer #1" in request.name:
            return "<promise>COMPLETE</promise>"
        raise RuntimeError("agent failed")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == [issues[0]]
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[1]
    assert isinstance(result.errors[0][1], RuntimeError)


def test_implement_phase_no_complete_tag_goes_to_errors(tmp_path):
    """When implementer output lacks COMPLETE tag, parse raises and issue goes to errors."""
    issues = [{"number": 1, "title": "Fix A"}]
    fake = FakeAgentRunner(["some response without the complete tag"])

    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert len(result.errors) == 1


# ── implement_phase: errors passed to logger ─────────────────────────────────


def test_implement_phase_logs_preflight_failure_via_logger(tmp_path):
    """PreflightFailure must be passed to deps.logger.log_error()."""
    issues = [{"number": 1, "title": "Fix A"}]
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    logger = RecordingLogger()
    fake = FakeAgentRunner([failure])

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert len(logger.errors) == 1
    assert logger.errors[0][0] == issues[0]
    assert logger.errors[0][1] is failure


def test_implement_phase_logs_exception_via_logger(tmp_path):
    """Exceptions raised during run_issue must be passed to deps.logger.log_error()."""
    issues = [{"number": 1, "title": "Fix A"}]
    boom = RuntimeError("agent crashed")
    logger = RecordingLogger()
    fake = FakeAgentRunner([boom])

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert len(logger.errors) == 1
    assert logger.errors[0][0] == issues[0]
    assert logger.errors[0][1] is boom


def test_implement_phase_successful_issues_not_logged_as_errors(tmp_path):
    """Completed issues must not produce log_error() calls."""
    issues = [{"number": 1, "title": "Fix A"}]
    logger = RecordingLogger()
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert logger.errors == []


def test_implement_phase_logs_implementer_output_on_success(tmp_path):
    """Implementer output is passed to deps.logger.log_agent_output() when it succeeds."""
    issues = [{"number": 7, "title": "Fix C"}]
    logger = RecordingLogger()
    agent_output = "<promise>COMPLETE</promise>"
    fake = FakeAgentRunner([agent_output, agent_output])

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert len(logger.agent_outputs) == 1
    assert logger.agent_outputs[0] == ("Implementer #7", agent_output)


def test_implement_phase_reviewer_usage_limit_signals_in_result(tmp_path):
    """When reviewer hits usage limit, implement_phase returns usage_limit_hit=True and issue is not completed."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _side_effect(request: RunRequest):
        if "Implementer" in request.name:
            return "<promise>COMPLETE</promise>"
        raise UsageLimitError("")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.usage_limit_hit is True
    assert result.completed == []
    assert result.errors == []


# ── run_issue: prompt args and skip_preflight ─────────────────────────────────


def test_run_issue_derives_branch_from_issue_number(tmp_path):
    """run_issue must derive the branch via branch_for(number) and include it in prompt_args."""
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)

    issue = {"number": 7, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(c for c in fake.calls if "Implementer" in c.name)
    assert implementer_call.branch == "pycastle/issue-7"
    assert implementer_call.prompt_args["BRANCH"] == "pycastle/issue-7"


def test_run_issue_passes_feedback_commands_to_implementer(tmp_path):
    """run_issue must include FEEDBACK_COMMANDS in prompt_args for the implementer."""
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(c for c in fake.calls if "Implementer" in c.name)
    assert "FEEDBACK_COMMANDS" in implementer_call.prompt_args


def test_run_issue_feedback_commands_include_backtick_wrapped_implement_checks(
    tmp_path,
):
    """FEEDBACK_COMMANDS must be formatted from IMPLEMENT_CHECKS with backtick wrapping."""
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(c for c in fake.calls if "Implementer" in c.name)
    feedback_commands = implementer_call.prompt_args["FEEDBACK_COMMANDS"]
    for cmd in _cfg.implement_checks:
        assert f"`{cmd}`" in feedback_commands


def test_run_issue_implementer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the implementer agent."""
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in fake.calls if "Implementer" in c.name)
    assert impl_call.skip_preflight is True


def test_run_issue_reviewer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the reviewer agent."""
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    rev_call = next(c for c in fake.calls if "Reviewer" in c.name)
    assert rev_call.skip_preflight is True


def test_run_issue_raises_when_implementer_does_not_complete(tmp_path):
    """run_issue must raise PromiseParseError when implementer lacks COMPLETE tag."""
    from pycastle.agent_output_protocol import PromiseParseError

    fake = FakeAgentRunner(["I tried but could not finish"])

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(PromiseParseError):
        asyncio.run(run_issue(issue, deps))


def test_run_issue_returns_issue_when_implementer_completes(tmp_path):
    """run_issue must return the issue dict when implementer produces COMPLETE."""
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)

    issue = {"number": 2, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue


# ── Cycle 274: AgentTimeoutError propagation through implement layer ──────────


def test_run_issue_raises_agent_timeout_error_when_implementer_exhausts_retries(
    tmp_path,
):
    """When implementer raises AgentTimeoutError, run_issue must propagate it."""

    async def _side_effect(request: RunRequest):
        if "Implementer" in request.name:
            raise AgentTimeoutError("timeout")
        return "<promise>COMPLETE</promise>"

    fake = FakeAgentRunner(side_effect=_side_effect)
    issue = {"number": 5, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run_issue(issue, deps))


def test_run_issue_raises_agent_timeout_error_when_reviewer_exhausts_retries(tmp_path):
    """When reviewer raises AgentTimeoutError, run_issue must propagate it."""

    async def _side_effect(request: RunRequest):
        if "Implementer" in request.name:
            return "<promise>COMPLETE</promise>"
        raise AgentTimeoutError("timeout")

    fake = FakeAgentRunner(side_effect=_side_effect)
    issue = {"number": 5, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run_issue(issue, deps))


def test_implement_phase_implementer_timeout_tracked_as_error(tmp_path):
    """When implementer raises AgentTimeoutError, implement_phase tracks the issue in errors."""
    issues = [{"number": 3, "title": "Fix C"}]

    async def _side_effect(request: RunRequest):
        if "Implementer" in request.name:
            raise AgentTimeoutError("timeout")
        return "<promise>COMPLETE</promise>"

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[0]
    assert isinstance(result.errors[0][1], AgentTimeoutError)


def test_implement_phase_reviewer_timeout_does_not_complete_issue(tmp_path):
    """When reviewer raises AgentTimeoutError, the issue must not appear in completed."""
    issues = [{"number": 4, "title": "Fix D"}]

    async def _side_effect(request: RunRequest):
        if "Implementer" in request.name:
            return "<promise>COMPLETE</promise>"
        raise AgentTimeoutError("timeout")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == []
    assert len(result.errors) == 1
    assert isinstance(result.errors[0][1], AgentTimeoutError)


# ── Issue 349: issue_title threading ─────────────────────────────────────────


def test_run_issue_passes_issue_title_to_implementer(tmp_path):
    issue = {"number": 5, "title": "Fix auth timeout"}
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)
    deps = _make_deps(tmp_path, fake)

    asyncio.run(run_issue(issue, deps))

    assert fake.calls[0].issue_title == "Fix auth timeout"


def test_run_issue_passes_issue_title_to_reviewer(tmp_path):
    issue = {"number": 5, "title": "Fix auth timeout"}
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"] * 2)
    deps = _make_deps(tmp_path, fake)

    asyncio.run(run_issue(issue, deps))

    assert fake.calls[1].issue_title == "Fix auth timeout"
