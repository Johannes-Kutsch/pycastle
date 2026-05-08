import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agent_output_protocol import (
    CommitMessageOutput,
    CompletionOutput,
    PromiseParseError,
)
from pycastle.agent_result import PreflightFailure
from pycastle.agent_runner import RunRequest
from pycastle.config import Config
from pycastle.errors import AgentTimeoutError, UsageLimitError
from pycastle.services import GitService
from pycastle.iteration._deps import (
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
)
from pycastle.status_display import PlainStatusDisplay, StatusDisplay
from pycastle.iteration.implement import (
    ImplementResult,
    branch_for,
    build_issue_scope_args,
    format_issue_comments,
    implement_phase,
    run_issue,
)

_cfg = Config()


@dataclasses.dataclass
class _ImplementStub:
    cfg: Config
    status_display: StatusDisplay
    agent_runner: FakeAgentRunner
    git_svc: GitService
    repo_root: Path
    logger: RecordingLogger


def _make_deps(
    tmp_path, agent_runner, logger=None, status_display=None
) -> _ImplementStub:
    import shutil as _shutil

    git_svc = MagicMock(spec=GitService)
    git_svc.verify_ref_exists.return_value = False

    _registered: list[Path] = []

    def _fake_list_worktrees(repo):
        return list(_registered)

    def _fake_create_worktree(repo, path, branch, sha=None):
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n")
        _registered.append(path)

    def _fake_remove_worktree(repo, path):
        _shutil.rmtree(path, ignore_errors=True)
        _registered[:] = [p for p in _registered if p != path]

    git_svc.list_worktrees.side_effect = _fake_list_worktrees
    git_svc.create_worktree.side_effect = _fake_create_worktree
    git_svc.remove_worktree.side_effect = _fake_remove_worktree
    return _ImplementStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=agent_runner,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger or RecordingLogger(),
        status_display=status_display or PlainStatusDisplay(),
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
        raise UsageLimitError(reset_time=None)

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, None, deps))

    assert result.usage_limit_hit is True


def test_implement_phase_usage_limit_does_not_exit(tmp_path):
    """implement_phase must not call sys.exit() when a usage limit is hit."""
    issues = [{"number": 1, "title": "Fix A"}]

    async def _side_effect(request: RunRequest):
        raise UsageLimitError(reset_time=None)

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
            raise UsageLimitError(reset_time=None)
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
        raise UsageLimitError(reset_time=None)

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
    assert implementer_call.scope_args["BRANCH"] == "pycastle/issue-7"
    branch_arg = deps.git_svc.create_worktree.call_args_list[0][0][2]
    assert branch_arg == "pycastle/issue-7"


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


# ── Issue 497: issue body/comments and diff threading ───────────────────────


def test_run_issue_threads_issue_body_to_implementer_prompt(tmp_path):
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    issue = {"number": 1, "title": "T", "body": "BODY-X", "comments": []}

    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    assert impl_call.scope_args["ISSUE_BODY"] == "BODY-X"


def test_run_issue_threads_issue_body_to_reviewer_prompt(tmp_path):
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    issue = {"number": 1, "title": "T", "body": "BODY-X", "comments": []}

    asyncio.run(run_issue(issue, deps))

    rev_call = next(c for c in fake.calls if "Review Agent" in c.name)
    assert rev_call.scope_args["ISSUE_BODY"] == "BODY-X"


def test_run_issue_threads_issue_comments_formatted_to_implementer(tmp_path):
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    issue = {
        "number": 1,
        "title": "T",
        "body": "",
        "comments": [
            {"author": "alice", "created_at": "2026-01-01T10:00:00Z", "body": "hi"},
            {"author": "bob", "created_at": "2026-01-02T11:00:00Z", "body": "yo"},
        ],
    }

    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    rendered = impl_call.scope_args["ISSUE_COMMENTS"]
    assert "alice" in rendered
    assert "2026-01-01T10:00:00Z" in rendered
    assert "hi" in rendered
    assert "bob" in rendered
    assert rendered.index("alice") < rendered.index("bob")


def test_run_issue_renders_empty_string_when_no_comments(tmp_path):
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    issue = {"number": 1, "title": "T", "body": "x", "comments": []}

    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    assert impl_call.scope_args["ISSUE_COMMENTS"] == ""


def test_run_issue_does_not_pass_diff_to_either_agent(tmp_path):
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    issue = {"number": 1, "title": "T", "body": "", "comments": []}

    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    rev_call = next(c for c in fake.calls if "Review Agent" in c.name)
    assert "DIFF" not in impl_call.scope_args
    assert "DIFF" not in rev_call.scope_args


def test_run_issue_handles_issue_without_body_or_comments(tmp_path):
    """AFK-path issues lack body/comments — prompt args must still be populated."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    issue = {"number": 1, "title": "T"}

    asyncio.run(run_issue(issue, deps))

    impl_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    assert impl_call.scope_args["ISSUE_BODY"] == ""
    assert impl_call.scope_args["ISSUE_COMMENTS"] == ""


def test_format_issue_comments_includes_author_and_timestamp():
    rendered = format_issue_comments(
        [
            {"author": "alice", "created_at": "2026-01-01T10:00:00Z", "body": "hi"},
        ]
    )
    assert "alice" in rendered
    assert "2026-01-01T10:00:00Z" in rendered
    assert "hi" in rendered


def test_format_issue_comments_returns_empty_string_for_no_comments():
    assert format_issue_comments([]) == ""


# ── build_issue_scope_args ────────────────────────────────────────────────────


def test_build_issue_scope_args_returns_all_required_keys():
    issue = {"number": 1, "title": "Fix bug", "body": "details", "comments": []}
    result = build_issue_scope_args(issue, "pycastle/issue-1")
    assert set(result.keys()) == {
        "ISSUE_NUMBER",
        "ISSUE_TITLE",
        "ISSUE_BODY",
        "ISSUE_COMMENTS",
        "BRANCH",
    }


def test_build_issue_scope_args_formats_number_as_string():
    issue = {"number": 42, "title": "T", "body": "", "comments": []}
    result = build_issue_scope_args(issue, "pycastle/issue-42")
    assert result["ISSUE_NUMBER"] == "42"


def test_build_issue_scope_args_uses_branch_arg():
    issue = {"number": 7, "title": "T", "body": "", "comments": []}
    result = build_issue_scope_args(issue, "pycastle/issue-7")
    assert result["BRANCH"] == "pycastle/issue-7"


def test_build_issue_scope_args_handles_missing_body_and_comments():
    issue = {"number": 1, "title": "T"}
    result = build_issue_scope_args(issue, "pycastle/issue-1")
    assert result["ISSUE_BODY"] == ""
    assert result["ISSUE_COMMENTS"] == ""


def test_build_issue_scope_args_formats_comments():
    issue = {
        "number": 1,
        "title": "T",
        "body": "",
        "comments": [
            {"author": "alice", "created_at": "2026-01-01T10:00:00Z", "body": "hi"}
        ],
    }
    result = build_issue_scope_args(issue, "pycastle/issue-1")
    assert "alice" in result["ISSUE_COMMENTS"]
    assert "hi" in result["ISSUE_COMMENTS"]


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

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            raise UsageLimitError(reset_time=None)
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 12, "title": "Fix thing"}
    with pytest.raises(UsageLimitError):
        asyncio.run(run_issue(issue, deps))

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


# ── run_issue: role-dir stage-done skip logic ─────────────────────────────────


def _seed_review_stage_done(tmp_path: Path, issue_number: int) -> None:
    """Create empty reviewer session dir to signal review stage done."""
    wt_path = tmp_path / "pycastle" / ".worktrees" / f"issue-{issue_number}"
    rev_dir = wt_path / ".pycastle-session" / "reviewer"
    rev_dir.mkdir(parents=True)


def _seed_implement_stage_done(tmp_path: Path, issue_number: int) -> None:
    """Create empty implementer session dir to signal implement stage done."""
    wt_path = tmp_path / "pycastle" / ".worktrees" / f"issue-{issue_number}"
    impl_dir = wt_path / ".pycastle-session" / "implementer"
    impl_dir.mkdir(parents=True)


def test_run_issue_review_skip_returns_issue_without_invoking_any_agent(tmp_path):
    """When reviewer stage-done signal is set, run_issue returns the issue without spawning agents."""
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    _seed_review_stage_done(tmp_path, 20)

    issue = {"number": 20, "title": "Fix auth"}
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue
    assert fake.calls == []


def test_run_issue_review_skip_creates_no_worktree(tmp_path):
    """When reviewer stage-done signal is set, no worktree is created."""
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    _seed_review_stage_done(tmp_path, 21)

    issue = {"number": 21, "title": "Fix auth"}
    asyncio.run(run_issue(issue, deps))

    deps.git_svc.create_worktree.assert_not_called()


def test_run_issue_implement_skip_invokes_only_reviewer(tmp_path):
    """When implementer stage-done signal is set, run_issue skips Implementer and runs only Reviewer."""
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    _seed_implement_stage_done(tmp_path, 22)

    issue = {"number": 22, "title": "Fix auth"}
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue
    assert len(fake.calls) == 1
    assert "Review Agent" in fake.calls[0].name


def test_run_issue_implement_skip_creates_no_implementer_worktree(tmp_path):
    """When implementer stage-done signal is set, no Implementer worktree is created."""
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    _seed_implement_stage_done(tmp_path, 23)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 23, "title": "Fix auth"}
    asyncio.run(run_issue(issue, deps))

    assert deps.git_svc.create_worktree.call_count == 1
    branch_arg = deps.git_svc.create_worktree.call_args[0][2]
    assert branch_arg == "pycastle/issue-23"


def test_run_issue_no_stage_done_signal_runs_both_agents(tmp_path):
    """When no stage-done signal exists, run_issue runs both Implementer and Reviewer normally."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    issue = {"number": 24, "title": "Fix auth"}
    result = asyncio.run(run_issue(issue, deps))

    assert result == issue
    assert len(fake.calls) == 2
    assert "Implement Agent" in fake.calls[0].name
    assert "Review Agent" in fake.calls[1].name


def test_run_issue_releases_lock_on_unexpected_exception(tmp_path):
    """If an exception is raised inside run_issue, the branch lock must be released."""

    async def _failing(_request):
        raise RuntimeError("boom")

    fake = FakeAgentRunner(side_effect=_failing)
    deps = _make_deps(tmp_path, fake)

    branch_locks: dict[str, asyncio.Lock] = {}
    issue = {"number": 25, "title": "Fix auth"}

    with pytest.raises(RuntimeError):
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
    """run_issue does not call on_started when reviewer stage-done signal is set."""
    fired: list[int] = []
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    _seed_review_stage_done(tmp_path, 1)

    issue = {"number": 1, "title": "Fix thing"}
    asyncio.run(run_issue(issue, deps, on_started=lambda: fired.append(1)))

    assert fired == []


# ── run_issue: commit wiring ─────────────────────────────────────────────────


def test_run_issue_commits_implementer_with_issue_number_and_message(tmp_path):
    """After Implementer returns CommitMessageOutput with message, commit uses 'Implement #N - <msg>'."""
    fake = FakeAgentRunner(
        [CommitMessageOutput(message="add foo"), CommitMessageOutput(message="tidy")]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 40, "title": "Fix"}
    asyncio.run(run_issue(issue, deps))

    impl_call = deps.git_svc.commit.call_args_list[0]
    assert impl_call[0][2] == "Implement #40 - add foo"


def test_run_issue_commits_implementer_with_title_when_no_commit_message_tag(tmp_path):
    """After Implementer returns CommitMessageOutput(message=None), commit uses issue title as fallback."""
    fake = FakeAgentRunner(
        [CommitMessageOutput(message=None), CommitMessageOutput(message=None)]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 43, "title": "Fix the login bug"}
    asyncio.run(run_issue(issue, deps))

    impl_call = deps.git_svc.commit.call_args_list[0]
    assert impl_call[0][2] == "Implement #43 - Fix the login bug"


def test_run_issue_commits_reviewer_with_issue_number_and_message(tmp_path):
    """After Reviewer returns CommitMessageOutput with message, commit uses 'Review #N - <msg>'."""
    fake = FakeAgentRunner(
        [
            CommitMessageOutput(message="add foo"),
            CommitMessageOutput(message="rename var"),
        ]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 41, "title": "Fix"}
    asyncio.run(run_issue(issue, deps))

    review_call = deps.git_svc.commit.call_args_list[1]
    assert review_call[0][2] == "Review #41 - rename var"


def test_run_issue_commits_reviewer_with_title_when_no_commit_message_tag(tmp_path):
    """After Reviewer returns CommitMessageOutput(message=None), commit uses issue title as fallback."""
    fake = FakeAgentRunner(
        [CommitMessageOutput(message=None), CommitMessageOutput(message=None)]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {"number": 44, "title": "Add dark mode"}
    asyncio.run(run_issue(issue, deps))

    review_call = deps.git_svc.commit.call_args_list[1]
    assert review_call[0][2] == "Review #44 - Add dark mode"


def test_run_issue_does_not_commit_on_preflight_failure(tmp_path):
    """If Implementer returns PreflightFailure, no commit must be made."""
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    fake = FakeAgentRunner([failure])
    deps = _make_deps(tmp_path, fake)

    issue = {"number": 42, "title": "Fix"}
    asyncio.run(run_issue(issue, deps))

    deps.git_svc.commit.assert_not_called()


def test_run_issue_on_started_fires_when_only_reviewer_runs(tmp_path):
    """run_issue calls on_started once when implement stage-done signal is set."""
    fired: list[int] = []
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    _seed_implement_stage_done(tmp_path, 1)

    issue = {"number": 1, "title": "Fix auth"}
    asyncio.run(run_issue(issue, deps, on_started=lambda: fired.append(1)))

    assert fired == [1]


# ── run_issue: role session cleanup after commit ──────────────────────────────


def test_run_issue_clears_implementer_session_dir_contents_after_commit(tmp_path):
    """After Implementer commits, session dir is cleared (not deleted), leaving the stage-done signal.

    The worktree is made dirty so it is preserved, making the session dir observable.
    """
    fake = FakeAgentRunner(
        [CommitMessageOutput(message="fix it"), CommitMessageOutput(message="tidy")]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = False  # preserve worktree

    wt_name = "issue-50"
    wt_path = tmp_path / deps.cfg.pycastle_dir / ".worktrees" / wt_name
    impl_session_dir = wt_path / ".pycastle-session" / "implementer"

    original_create = deps.git_svc.create_worktree.side_effect

    def _seeding_create(repo, path, branch, sha=None):
        original_create(repo, path, branch, sha)
        if not impl_session_dir.is_dir():
            impl_session_dir.mkdir(parents=True, exist_ok=True)
            (impl_session_dir / "session.json").write_text("{}")

    deps.git_svc.create_worktree.side_effect = _seeding_create

    issue = {"number": 50, "title": "Fix"}
    asyncio.run(run_issue(issue, deps))

    # Dir exists (not removed) but is empty (contents cleared = stage-done signal).
    assert impl_session_dir.is_dir()
    assert not any(impl_session_dir.iterdir())


def test_run_issue_clears_reviewer_session_dir_contents_after_commit(tmp_path):
    """After Reviewer commits, session dir is cleared (not deleted), leaving the stage-done signal.

    The worktree is made dirty so it is preserved, making the session dir observable.
    """
    fake = FakeAgentRunner(
        [CommitMessageOutput(message="fix it"), CommitMessageOutput(message="tidy")]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = False  # preserve worktree

    wt_name = "issue-51"
    wt_path = tmp_path / deps.cfg.pycastle_dir / ".worktrees" / wt_name
    rev_session_dir = wt_path / ".pycastle-session" / "reviewer"

    original_create = deps.git_svc.create_worktree.side_effect

    def _seeding_create(repo, path, branch, sha=None):
        original_create(repo, path, branch, sha)
        if not rev_session_dir.is_dir():
            rev_session_dir.mkdir(parents=True, exist_ok=True)
            (rev_session_dir / "session.json").write_text("{}")

    deps.git_svc.create_worktree.side_effect = _seeding_create

    issue = {"number": 51, "title": "Fix"}
    asyncio.run(run_issue(issue, deps))

    assert rev_session_dir.is_dir()
    assert not any(rev_session_dir.iterdir())
