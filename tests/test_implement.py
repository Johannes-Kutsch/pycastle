import asyncio
from unittest.mock import MagicMock

import pytest

from pycastle.agent_output_protocol import CompletionOutput, PromiseParseError
from pycastle.agent_result import (
    CancellationToken,
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
    RecordingLogger,
    RecordingStatusDisplay,
)
from pycastle.status_display import PlainStatusDisplay
from pycastle.iteration.implement import (
    ImplementResult,
    _agent_worktree,
    branch_for,
    implement_phase,
    run_issue,
)

_cfg = Config()


def _make_deps(tmp_path, agent_runner, logger=None, status_display=None) -> Deps:
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=MagicMock(spec=GitService),
        github_svc=MagicMock(spec=GithubService),
        agent_runner=agent_runner,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger or RecordingLogger(),
        status_display=status_display or PlainStatusDisplay(),  # type: ignore[arg-type]
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
    fake = FakeAgentRunner([CompletionOutput()] * 4)

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
        if "Implement Agent #1" in request.name:
            raise UsageLimitError("")
        completed_agents.append(request.name)
        return CompletionOutput()

    issues = [{"number": 1, "title": "Fail"}, {"number": 2, "title": "Pass"}]
    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    asyncio.run(implement_phase(issues, None, deps))

    assert any("Implement Agent #2" in n for n in completed_agents), (
        f"Sibling Implement Agent #2 must complete before returning; completed={completed_agents}"
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
        if "Implement Agent #1" in request.name or "Review Agent #1" in request.name:
            return CompletionOutput()
        raise RuntimeError("agent failed")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.completed == [issues[0]]
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[1]
    assert isinstance(result.errors[0][1], RuntimeError)


def test_implement_phase_no_complete_tag_goes_to_errors(tmp_path):
    """When implementer raises PromiseParseError, issue goes to errors."""
    issues = [{"number": 1, "title": "Fix A"}]
    fake = FakeAgentRunner([PromiseParseError("no <promise>COMPLETE</promise> tag")])

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
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert logger.errors == []


def test_implement_phase_does_not_log_implementer_output(tmp_path):
    """Implementer output is no longer logged via log_agent_output (raw string unavailable)."""
    issues = [{"number": 7, "title": "Fix C"}]
    logger = RecordingLogger()
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, None, deps))

    assert logger.agent_outputs == []


def test_implement_phase_reviewer_usage_limit_signals_in_result(tmp_path):
    """When reviewer hits usage limit, implement_phase returns usage_limit_hit=True and issue is not completed."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        raise UsageLimitError("")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.usage_limit_hit is True
    assert result.completed == []
    assert result.errors == []


# ── run_issue: prompt args and skip_preflight ─────────────────────────────────


def test_run_issue_derives_branch_from_issue_number(tmp_path):
    """run_issue must derive the branch via branch_for(number) and pass it to create_worktree and prompt_args."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {"number": 7, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    assert implementer_call.prompt_args["BRANCH"] == "pycastle/issue-7"
    branch_arg = deps.git_svc.create_worktree.call_args_list[0][0][2]
    assert branch_arg == "pycastle/issue-7"


def test_run_issue_passes_feedback_commands_to_implementer(tmp_path):
    """run_issue must include FEEDBACK_COMMANDS in prompt_args for the implementer."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    assert "FEEDBACK_COMMANDS" in implementer_call.prompt_args


def test_run_issue_feedback_commands_include_backtick_wrapped_implement_checks(
    tmp_path,
):
    """FEEDBACK_COMMANDS must be formatted from IMPLEMENT_CHECKS with backtick wrapping."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    implementer_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    feedback_commands = implementer_call.prompt_args["FEEDBACK_COMMANDS"]
    for cmd in _cfg.implement_checks:
        assert f"`{cmd}`" in feedback_commands


def test_run_issue_implementer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the implementer agent."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    assert impl_call.skip_preflight is True


def test_run_issue_reviewer_invoked_with_skip_preflight_true(tmp_path):
    """run_issue must pass skip_preflight=True to the reviewer agent."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps))

    rev_call = next(c for c in fake.calls if "Review Agent" in c.name)
    assert rev_call.skip_preflight is True


def test_run_issue_raises_when_implementer_does_not_complete(tmp_path):
    """run_issue must raise PromiseParseError when implementer lacks COMPLETE tag."""
    fake = FakeAgentRunner([PromiseParseError("no <promise>COMPLETE</promise> tag")])

    issue = {"number": 1, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(PromiseParseError):
        asyncio.run(run_issue(issue, deps))


def test_run_issue_returns_issue_when_implementer_completes(tmp_path):
    """run_issue must return the issue dict when implementer produces COMPLETE."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

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
        if "Implement Agent" in request.name:
            raise AgentTimeoutError("timeout")
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_side_effect)
    issue = {"number": 5, "title": "Fix thing"}
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run_issue(issue, deps))


def test_run_issue_raises_agent_timeout_error_when_reviewer_exhausts_retries(tmp_path):
    """When reviewer raises AgentTimeoutError, run_issue must propagate it."""

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
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
        if "Implement Agent" in request.name:
            raise AgentTimeoutError("timeout")
        return CompletionOutput()

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
        if "Implement Agent" in request.name:
            return CompletionOutput()
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
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    asyncio.run(run_issue(issue, deps))

    assert fake.calls[0].issue_title == "Fix auth timeout"


def test_run_issue_passes_issue_title_to_reviewer(tmp_path):
    issue = {"number": 5, "title": "Fix auth timeout"}
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    asyncio.run(run_issue(issue, deps))

    assert fake.calls[1].issue_title == "Fix auth timeout"


# ── _agent_worktree ────────────────────────────────────────────────────────────


def _make_overlay(tmp_path):
    p = tmp_path / "gitdir_overlay"
    p.write_text("gitdir: /.pycastle-parent-git/worktrees/issue-1\n")
    return p


def test_agent_worktree_creates_worktree_on_entry_and_removes_on_clean_exit(
    tmp_path, monkeypatch
):
    """_agent_worktree calls create_worktree on entry and remove_worktree on clean exit."""
    overlay = _make_overlay(tmp_path)
    monkeypatch.setattr(
        "pycastle.iteration.implement.patch_gitdir_for_container", lambda p: overlay
    )
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    deps.git_svc.is_working_tree_clean.return_value = True
    token = CancellationToken()

    async def _run():
        async with _agent_worktree("pycastle/issue-1", "abc123", token, deps):
            pass

    asyncio.run(_run())

    deps.git_svc.create_worktree.assert_called_once()
    args = deps.git_svc.create_worktree.call_args[0]
    assert args[2] == "pycastle/issue-1"
    assert args[3] == "abc123"
    deps.git_svc.remove_worktree.assert_called_once()
    assert not overlay.exists()


def test_agent_worktree_preserves_worktree_when_token_wants_preserved(
    tmp_path, monkeypatch
):
    """_agent_worktree skips remove_worktree when token.wants_worktree_preserved is True."""
    overlay = _make_overlay(tmp_path)
    monkeypatch.setattr(
        "pycastle.iteration.implement.patch_gitdir_for_container", lambda p: overlay
    )
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    token = CancellationToken()
    token.cancel(preserve_worktree=True)

    async def _run():
        async with _agent_worktree("pycastle/issue-2", None, token, deps):
            pass

    asyncio.run(_run())

    deps.git_svc.remove_worktree.assert_not_called()
    assert not overlay.exists()


def test_agent_worktree_preserves_worktree_when_dirty(tmp_path, monkeypatch):
    """_agent_worktree skips remove_worktree when the working tree is dirty."""
    overlay = _make_overlay(tmp_path)
    monkeypatch.setattr(
        "pycastle.iteration.implement.patch_gitdir_for_container", lambda p: overlay
    )
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    deps.git_svc.is_working_tree_clean.return_value = False
    token = CancellationToken()

    async def _run():
        async with _agent_worktree("pycastle/issue-3", None, token, deps):
            pass

    asyncio.run(_run())

    deps.git_svc.remove_worktree.assert_not_called()
    assert not overlay.exists()


def test_agent_worktree_always_removes_gitdir_overlay(tmp_path, monkeypatch):
    """_agent_worktree removes the gitdir overlay on exit even when the worktree is preserved."""
    overlay = _make_overlay(tmp_path)
    monkeypatch.setattr(
        "pycastle.iteration.implement.patch_gitdir_for_container", lambda p: overlay
    )
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    token = CancellationToken()
    token.cancel(preserve_worktree=True)

    async def _run():
        async with _agent_worktree("pycastle/issue-4", None, token, deps):
            pass

    asyncio.run(_run())

    assert not overlay.exists()


def test_agent_worktree_removes_gitdir_overlay_even_when_body_raises(
    tmp_path, monkeypatch
):
    """_agent_worktree removes the gitdir overlay even when the body raises."""
    overlay = _make_overlay(tmp_path)
    monkeypatch.setattr(
        "pycastle.iteration.implement.patch_gitdir_for_container", lambda p: overlay
    )
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    deps.git_svc.is_working_tree_clean.return_value = True
    token = CancellationToken()

    async def _run():
        async with _agent_worktree("pycastle/issue-5", None, token, deps):
            raise RuntimeError("body failure")

    with pytest.raises(RuntimeError, match="body failure"):
        asyncio.run(_run())

    assert not overlay.exists()


# ── run_issue: worktree lifecycle ─────────────────────────────────────────────


def test_run_issue_creates_two_worktrees_implementer_and_reviewer(tmp_path):
    """run_issue must call create_worktree twice: once for the Implementer, once for the Reviewer."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 10, "title": "Fix thing"}
    asyncio.run(run_issue(issue, deps))

    assert deps.git_svc.create_worktree.call_count == 2


def test_run_issue_removes_worktrees_after_successful_run(tmp_path):
    """run_issue must remove both worktrees when the working tree is clean."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 11, "title": "Fix thing"}
    asyncio.run(run_issue(issue, deps))

    assert deps.git_svc.remove_worktree.call_count == 2


def test_run_issue_preserves_worktree_on_usage_limit(tmp_path):
    """run_issue must not remove the Implementer worktree when usage limit is hit."""
    token = CancellationToken()

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            token.cancel(preserve_worktree=True)
            raise UsageLimitError("")
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 12, "title": "Fix thing"}
    with pytest.raises(UsageLimitError):
        asyncio.run(run_issue(issue, deps, token=token))

    deps.git_svc.remove_worktree.assert_not_called()


def test_run_issue_preserves_worktree_when_dirty(tmp_path):
    """run_issue must not remove the worktree when the working tree is dirty, but still return the issue."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = False

    issue = {"number": 13, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue
    deps.git_svc.remove_worktree.assert_not_called()


def test_run_issue_raises_branch_collision_for_concurrent_same_issue(tmp_path):
    """run_issue raises BranchCollisionError when two calls race on the same issue number."""
    from pycastle.errors import BranchCollisionError

    async def _yielding_side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            await asyncio.sleep(0)  # yield so Task 2 can observe the held lock
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_yielding_side_effect)
    deps = _make_deps(tmp_path, fake)
    branch_locks: dict[str, asyncio.Lock] = {}
    issue = {"number": 14, "title": "Fix thing"}

    async def _two_concurrent():
        return await asyncio.gather(
            run_issue(issue, deps, branch_locks=branch_locks),
            run_issue(issue, deps, branch_locks=branch_locks),
            return_exceptions=True,
        )

    results = asyncio.run(_two_concurrent())
    errors = [r for r in results if isinstance(r, Exception)]
    assert any(isinstance(e, BranchCollisionError) for e in errors)


def test_run_issue_does_not_create_reviewer_worktree_on_preflight_failure(tmp_path):
    """When the Implementer returns PreflightFailure, run_issue must skip the Reviewer worktree."""
    failure = PreflightFailure(failures=(("mypy", "mypy .", "error: missing module"),))
    fake = FakeAgentRunner([failure])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 15, "title": "Fix thing"}
    result = asyncio.run(run_issue(issue, deps))

    assert isinstance(result, PreflightFailure)
    assert deps.git_svc.create_worktree.call_count == 1


# ── run_issue: RALPH commit prefix skip logic ────────────────────────────────


def test_run_issue_review_skip_returns_issue_without_invoking_any_agent(tmp_path):
    """When branch has a RALPH: Review - commit, run_issue returns the issue without spawning agents."""
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.get_branch_commit_subjects.return_value = ["RALPH: Review - fix auth"]

    issue = {"number": 20, "title": "Fix auth"}
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue
    assert fake.calls == []


def test_run_issue_review_skip_creates_no_worktree(tmp_path):
    """When branch has a RALPH: Review - commit, no worktree is created."""
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.get_branch_commit_subjects.return_value = ["RALPH: Review - fix auth"]

    issue = {"number": 21, "title": "Fix auth"}
    asyncio.run(run_issue(issue, deps))

    deps.git_svc.create_worktree.assert_not_called()


def test_run_issue_implement_skip_invokes_only_reviewer(tmp_path):
    """When branch has a RALPH: (non-review) commit, run_issue skips Implementer and runs only Reviewer."""
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.get_branch_commit_subjects.return_value = ["RALPH: Fix auth"]

    issue = {"number": 22, "title": "Fix auth"}
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue
    assert len(fake.calls) == 1
    assert "Review Agent" in fake.calls[0].name


def test_run_issue_implement_skip_creates_no_implementer_worktree(tmp_path):
    """When branch has a RALPH: (non-review) commit, no Implementer worktree is created."""
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.get_branch_commit_subjects.return_value = ["RALPH: Fix auth"]
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 23, "title": "Fix auth"}
    asyncio.run(run_issue(issue, deps))

    assert deps.git_svc.create_worktree.call_count == 1
    branch_arg = deps.git_svc.create_worktree.call_args[0][2]
    assert branch_arg == "pycastle/issue-23"


def test_run_issue_no_ralph_commit_runs_both_agents(tmp_path):
    """When branch has no RALPH: commit, run_issue runs both Implementer and Reviewer normally."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.get_branch_commit_subjects.return_value = []

    issue = {"number": 24, "title": "Fix auth"}
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue
    assert len(fake.calls) == 2
    assert "Implement Agent" in fake.calls[0].name
    assert "Review Agent" in fake.calls[1].name


def test_run_issue_releases_lock_when_get_branch_commit_subjects_raises(tmp_path):
    """If get_branch_commit_subjects raises, run_issue must still release the branch lock."""
    from pycastle.services import GitTimeoutError

    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.get_branch_commit_subjects.side_effect = GitTimeoutError("timed out")

    branch_locks: dict[str, asyncio.Lock] = {}
    issue = {"number": 25, "title": "Fix auth"}

    with pytest.raises(GitTimeoutError):
        asyncio.run(run_issue(issue, deps, branch_locks=branch_locks))

    assert not branch_locks["pycastle/issue-25"].locked()


def test_run_issue_reviewer_worktree_uses_no_sha(tmp_path):
    """run_issue must create the Reviewer worktree without a pinned SHA (existing-branch path)."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 16, "title": "Fix thing"}
    asyncio.run(run_issue(issue, deps, sha="abc123"))

    assert deps.git_svc.create_worktree.call_count == 2
    reviewer_sha = deps.git_svc.create_worktree.call_args_list[1][0][3]
    assert reviewer_sha is None


# ── Issue 437: live agent-start progress counter ──────────────────────────────


def test_implement_phase_sets_initial_progress_text(tmp_path):
    """implement_phase registers 'Running: started Agents for 0/Y issues' before any agent runs."""
    issues = [{"number": 1, "title": "A"}, {"number": 2, "title": "B"}]
    fake = FakeAgentRunner([CompletionOutput()] * 4)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, None, deps))

    update_phase_calls = [
        c for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    ]
    assert update_phase_calls[0] == (
        "update_phase",
        "Implement",
        "Running: started Agents for 0/2 issues",
    )


def test_implement_phase_increments_progress_text_per_semaphore_acquisition(tmp_path):
    """implement_phase increments the counter each time a new issue acquires the semaphore."""
    issues = [
        {"number": 1, "title": "A"},
        {"number": 2, "title": "B"},
        {"number": 3, "title": "C"},
    ]
    fake = FakeAgentRunner([CompletionOutput()] * 6)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, None, deps))

    update_phase_calls = [
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    ]
    assert "Running: started Agents for 0/3 issues" in update_phase_calls
    assert "Running: started Agents for 1/3 issues" in update_phase_calls
    assert "Running: started Agents for 2/3 issues" in update_phase_calls
    assert "Running: started Agents for 3/3 issues" in update_phase_calls


def test_implement_phase_progress_total_matches_issue_count(tmp_path):
    """Y in the progress text equals the number of issues passed to implement_phase."""
    issues = [{"number": i, "title": f"Issue {i}"} for i in range(1, 6)]
    fake = FakeAgentRunner([CompletionOutput()] * 10)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, None, deps))

    initial = next(
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    )
    assert initial == "Running: started Agents for 0/5 issues"


def test_implement_phase_counter_is_monotonic(tmp_path):
    """Counter in progress text only increases and never decrements."""
    issues = [{"number": i, "title": f"Issue {i}"} for i in range(1, 4)]
    fake = FakeAgentRunner([CompletionOutput()] * 6)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, None, deps))

    counts = [
        int(c[2].split("for ")[1].split("/")[0])
        for c in sd.calls
        if c[0] == "update_phase" and c[1] == "Implement"
    ]
    assert counts == sorted(counts)


def test_run_issue_calls_on_started_once_per_issue(tmp_path):
    """run_issue calls on_started exactly once regardless of how many agents run."""
    fired: list[int] = []
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, deps, on_started=lambda: fired.append(1)))

    assert fired == [1]


def test_run_issue_on_started_not_called_when_review_already_done(tmp_path):
    """run_issue does not call on_started when review skip path is taken (no semaphore acquired)."""
    fired: list[int] = []
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.get_branch_commit_subjects.return_value = ["RALPH: Review - fix auth"]

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, deps, on_started=lambda: fired.append(1)))

    assert fired == []
