import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import (
    AgentRole,
    CommitMessageOutput,
    CompletionOutput,
    PromiseParseError,
)
from pycastle.agents.runner import RunRequest
from pycastle.config import Config, StageOverride
from pycastle.infrastructure.worktree import worktree_identity
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.errors import AgentTimeoutError, UsageLimitError
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.session import RoleSession
from pycastle.session.service_session_store import save_service_session_metadata
from pycastle.iteration.implement import (
    ImplementResult,
    branch_for,
    implement_phase,
    pick_implement_template,
    pick_slice_mode,
    run_issue,
)
from pycastle.services import GitService, GithubService
from pycastle.services import ServiceRegistry
from tests.support import (
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
    _make_deps,
)

_cfg = Config()


@dataclasses.dataclass(frozen=True)
class _CrossServiceTestService:
    name: str

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        relpath = f".pycastle-session/{role.value}/{self.name}"
        return f"{relpath}/{namespace}" if namespace else relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(state_dir.iterdir())


def _seed_prior_role_session_with_service(
    worktree: Path,
    *,
    role: AgentRole,
    service_name: str,
    session_id: str,
) -> None:
    role_session = RoleSession(worktree, role)
    role_session.start_fresh()
    (role_session.path / "_continuation").write_text(
        "opaque-continuation",
        encoding="utf-8",
    )
    role_session.save_service_session_id(service_name, session_id)
    save_service_session_metadata(role_session.path, service_name, session_id)
    state_dir = worktree / f".pycastle-session/{role.value}/{service_name}"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "seed").write_text("seed", encoding="utf-8")


def _mark_role_stage_done(worktree: Path, *, role: AgentRole) -> None:
    role_session = RoleSession(worktree, role)
    role_session.start_fresh()
    role_session.clear_provider_state_and_signal_completion()


def _reviewer_output(message: str | None) -> CommitMessageOutput:
    return CommitMessageOutput(message=message)


# ── branch_for ────────────────────────────────────────────────────────────────


def test_branch_for_returns_pycastle_issue_format():
    assert branch_for(193) == "pycastle/issue-193"


def test_branch_for_uses_issue_number():
    assert branch_for(1) == "pycastle/issue-1"
    assert branch_for(42) == "pycastle/issue-42"


# ── pick_implement_template ───────────────────────────────────────────────────


def test_pick_implement_template_behavior_slice_resolves_to_behavior_template():
    from pycastle.prompts.pipeline import PromptTemplate

    issue = {"number": 1, "title": "T", "labels": ["behavior-slice"]}
    assert pick_implement_template(issue, _cfg) == PromptTemplate.IMPLEMENT_BEHAVIOR


def test_pick_implement_template_refactor_slice_resolves_to_refactor_template():
    from pycastle.prompts.pipeline import PromptTemplate

    issue = {"number": 1, "title": "T", "labels": ["refactor-slice"]}
    assert pick_implement_template(issue, _cfg) == PromptTemplate.IMPLEMENT_REFACTOR


def test_pick_implement_template_docs_slice_resolves_to_docs_template():
    from pycastle.prompts.pipeline import PromptTemplate

    issue = {"number": 1, "title": "T", "labels": ["docs-slice"]}
    assert pick_implement_template(issue, _cfg) == PromptTemplate.IMPLEMENT_DOCS


def test_pick_implement_template_ignores_unrelated_labels():
    from pycastle.prompts.pipeline import PromptTemplate

    issue = {
        "number": 1,
        "title": "T",
        "labels": ["bug", "ready-for-agent", "behavior-slice"],
    }
    assert pick_implement_template(issue, _cfg) == PromptTemplate.IMPLEMENT_BEHAVIOR


def test_pick_implement_template_uses_carried_readiness_mode_over_labels():
    from pycastle.issue_readiness import (
        IssueReadiness,
        IssueReadinessKind,
        SliceMode,
        WellFormed,
        WellFormedBody,
    )
    from pycastle.prompts.pipeline import PromptTemplate

    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.REFACTOR, label="refactor-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=SliceMode.REFACTOR,
        kind=IssueReadinessKind.READY_AFK,
    )
    # Labels say docs-slice but the carried readiness result says refactor
    issue = {
        "number": 1,
        "title": "T",
        "labels": ["docs-slice"],
        "readiness": readiness,
    }

    assert pick_implement_template(issue, _cfg) == PromptTemplate.IMPLEMENT_REFACTOR


def test_pick_implement_template_prefers_carried_ready_outcome_over_fallback_mode():
    from pycastle.issue_readiness import (
        IssueReadiness,
        IssueReadinessKind,
        ReadyIssueOutcome,
        SliceMode,
        WellFormed,
        WellFormedBody,
    )

    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.REFACTOR, label="refactor-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=None,
        ready=ReadyIssueOutcome(
            display_name="docs",
            template=PromptTemplate.IMPLEMENT_DOCS,
        ),
        kind=IssueReadinessKind.READY_AFK,
    )
    issue = {
        "number": 1,
        "title": "T",
        "labels": ["behavior-slice"],
        "readiness": readiness,
    }

    assert pick_implement_template(issue, _cfg) == PromptTemplate.IMPLEMENT_DOCS


def test_pick_slice_mode_prefers_carried_ready_outcome_over_fallback_mode():
    from pycastle.issue_readiness import (
        IssueReadiness,
        IssueReadinessKind,
        ReadyIssueOutcome,
        SliceMode,
        WellFormed,
        WellFormedBody,
    )

    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.REFACTOR, label="refactor-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=None,
        ready=ReadyIssueOutcome(
            display_name="docs",
            template=PromptTemplate.IMPLEMENT_DOCS,
        ),
        kind=IssueReadinessKind.READY_AFK,
    )
    issue = {
        "number": 1,
        "title": "T",
        "labels": ["behavior-slice"],
        "readiness": readiness,
    }

    assert pick_slice_mode(issue, _cfg) == "docs"


def test_pick_implement_template_raises_runtime_error_for_malformed_issue():
    issue = {"number": 42, "title": "Bad", "labels": []}

    with pytest.raises(RuntimeError, match="not implement-ready"):
        pick_implement_template(issue, _cfg)


def test_pick_implement_template_raises_runtime_error_for_multiple_slice_labels():
    issue = {
        "number": 99,
        "title": "Ambiguous",
        "labels": ["behavior-slice", "refactor-slice"],
    }

    with pytest.raises(RuntimeError, match="not implement-ready"):
        pick_implement_template(issue, _cfg)


# ── implement_phase: parallel execution (tracer bullet) ───────────────────────


def test_implement_phase_returns_completed_issues(tmp_path):
    """implement_phase returns all issues in completed when every agent returns COMPLETE."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([CompletionOutput()] * 4)

    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert result.completed == issues
    assert result.errors == []
    assert result.usage_limit_hit is False


def test_implement_phase_empty_issues_returns_empty_result(tmp_path):
    """implement_phase with no issues makes no agent calls and returns an empty result."""
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase([], deps, "sha-abc"))

    assert result.completed == []
    assert result.errors == []
    assert result.usage_limit_hit is False
    assert fake.calls == []


def test_implement_phase_rejects_missing_slice_mode_before_progress_update(tmp_path):
    issues = [
        {
            "number": 1,
            "title": "Malformed",
            "body": "x" * 100,
            "comments": [],
            "labels": [],
        }
    ]
    fake = FakeAgentRunner([])
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    with pytest.raises(
        RuntimeError,
        match=(
            r"Issue #1 is not implement-ready: missing a ready "
            r"slice-mode selection\."
        ),
    ):
        asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert [call for call in sd.calls if call[0] == "update_phase"] == []
    assert fake.calls == []


def test_implement_phase_rejects_multiple_slice_modes_before_progress_update(
    tmp_path,
):
    issues = [
        {
            "number": 1,
            "title": "Malformed",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice", "docs-slice"],
        }
    ]
    fake = FakeAgentRunner([])
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    with pytest.raises(
        RuntimeError,
        match=(
            r"Issue #1 is not implement-ready: missing a ready "
            r"slice-mode selection\."
        ),
    ):
        asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert [call for call in sd.calls if call[0] == "update_phase"] == []
    assert fake.calls == []


# ── implement_phase: usage-limit signalling ───────────────────────────────────


def test_implement_phase_signals_usage_limit_in_result(tmp_path):
    """implement_phase returns usage_limit_hit=True instead of calling sys.exit."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _side_effect(request: RunRequest):
        raise UsageLimitError(reset_time=None)

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert result.usage_limit_hit is True


def test_implement_phase_usage_limit_does_not_exit(tmp_path):
    """implement_phase must not call sys.exit() when a usage limit is hit."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _side_effect(request: RunRequest):
        raise UsageLimitError(reset_time=None)

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)

    # Should not raise SystemExit
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))
    assert isinstance(result, ImplementResult)


def test_implement_phase_usage_limit_awaits_siblings(tmp_path):
    """When one issue hits usage limit, sibling tasks must complete before returning."""
    completed_agents: list[str] = []

    async def _side_effect(request: RunRequest):
        if "Implement Agent #1" in request.name:
            raise UsageLimitError(reset_time=None)
        completed_agents.append(request.name)
        return CompletionOutput()

    issues = [
        {
            "number": 1,
            "title": "Fail",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Pass",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert any("Implement Agent #2" in n for n in completed_agents), (
        f"Sibling Implement Agent #2 must complete before returning; completed={completed_agents}"
    )


# ── implement_phase: per-issue error collection ───────────────────────────────


def test_implement_phase_exception_goes_to_errors(tmp_path):
    """An exception raised by run_agent lands in result.errors."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "Fix B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]

    async def _side_effect(request: RunRequest):
        if "Implement Agent #1" in request.name or "Review Agent #1" in request.name:
            return CompletionOutput()
        raise RuntimeError("agent failed")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert result.completed == [issues[0]]
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[1]
    assert isinstance(result.errors[0][1], RuntimeError)


def test_implement_phase_no_complete_tag_goes_to_errors(tmp_path):
    """When implementer raises PromiseParseError, issue goes to errors."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    fake = FakeAgentRunner([PromiseParseError("no <promise>COMPLETE</promise> tag")])

    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert result.completed == []
    assert len(result.errors) == 1


# ── implement_phase: errors passed to logger ─────────────────────────────────


def test_implement_phase_logs_exception_via_logger(tmp_path):
    """Exceptions raised during run_issue must be passed to deps.logger.log_error()."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    boom = RuntimeError("agent crashed")
    logger = RecordingLogger()
    fake = FakeAgentRunner([boom])

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert len(logger.errors) == 1
    assert logger.errors[0][0] == issues[0]
    assert logger.errors[0][1] is boom


def test_implement_phase_successful_issues_not_logged_as_errors(tmp_path):
    """Completed issues must not produce log_error() calls."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    logger = RecordingLogger()
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert logger.errors == []


def test_implement_phase_does_not_log_implementer_output(tmp_path):
    """Implementer output is no longer logged via log_agent_output (raw string unavailable)."""
    issues = [
        {
            "number": 7,
            "title": "Fix C",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    logger = RecordingLogger()
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])

    deps = _make_deps(tmp_path, fake, logger=logger)
    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert logger.agent_outputs == []


def test_implement_phase_reviewer_usage_limit_signals_in_result(tmp_path):
    """When reviewer hits usage limit, implement_phase returns usage_limit_hit=True and issue is not completed."""
    issues = [
        {
            "number": 1,
            "title": "Fix A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        raise UsageLimitError(reset_time=None)

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert result.usage_limit_hit is True
    assert result.completed == []
    assert result.errors == []


# ── run_issue: prompt args and skip_preflight ─────────────────────────────────


def test_run_issue_derives_branch_from_issue_number(tmp_path):
    """run_issue must derive the branch via branch_for(number) and pass it to create_worktree."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {
        "number": 7,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    implementer_call = next(c for c in fake.calls if "Implement Agent" in c.name)
    assert implementer_call.prompt.template == PromptTemplate.IMPLEMENT_BEHAVIOR
    assert implementer_call.role == AgentRole.IMPLEMENTER
    assert implementer_call.stage == "pre-implementation"
    branch_arg = deps.git_svc.create_worktree.call_args_list[0][0][2]
    assert branch_arg == "pycastle/issue-7"


def test_run_issue_uses_issue_worktree_mount_path_for_both_agents(tmp_path):
    """run_issue keeps the issue branch mounted at the project-local issue-N path."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {
        "number": 7,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake)
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    expected_path = tmp_path / "pycastle" / ".worktrees" / "issue-7"
    assert deps.git_svc.create_worktree.call_count == 2
    assert deps.git_svc.create_worktree.call_args_list[0][0][1] == expected_path
    assert deps.git_svc.create_worktree.call_args_list[0][0][2] == "pycastle/issue-7"
    assert deps.git_svc.create_worktree.call_args_list[1][0][1] == expected_path
    assert deps.git_svc.create_worktree.call_args_list[1][0][2] == "pycastle/issue-7"


def test_run_issue_raises_when_implementer_does_not_complete(tmp_path):
    """run_issue must raise PromiseParseError when implementer lacks COMPLETE tag."""
    fake = FakeAgentRunner([PromiseParseError("no <promise>COMPLETE</promise> tag")])

    issue = {
        "number": 1,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(PromiseParseError):
        asyncio.run(run_issue(issue, deps, "sha-abc"))


def test_run_issue_returns_issue_when_implementer_completes(tmp_path):
    """run_issue must return the issue dict when implementer produces COMPLETE."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)

    issue = {
        "number": 2,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(run_issue(issue, deps, "sha-abc"))

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
    issue = {
        "number": 5,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run_issue(issue, deps, "sha-abc"))


def test_run_issue_raises_agent_timeout_error_when_reviewer_exhausts_retries(tmp_path):
    """When reviewer raises AgentTimeoutError, run_issue must propagate it."""

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        raise AgentTimeoutError("timeout")

    fake = FakeAgentRunner(side_effect=_side_effect)
    issue = {
        "number": 5,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(run_issue(issue, deps, "sha-abc"))


def test_implement_phase_implementer_timeout_tracked_as_error(tmp_path):
    """When implementer raises AgentTimeoutError, implement_phase tracks the issue in errors."""
    issues = [
        {
            "number": 3,
            "title": "Fix C",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            raise AgentTimeoutError("timeout")
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert result.completed == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == issues[0]
    assert isinstance(result.errors[0][1], AgentTimeoutError)


def test_implement_phase_reviewer_timeout_does_not_complete_issue(tmp_path):
    """When reviewer raises AgentTimeoutError, the issue must not appear in completed."""
    issues = [
        {
            "number": 4,
            "title": "Fix D",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            return CompletionOutput()
        raise AgentTimeoutError("timeout")

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    result = asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert result.completed == []
    assert len(result.errors) == 1
    assert isinstance(result.errors[0][1], AgentTimeoutError)


# ── Issue 497: issue body/comments and diff threading ───────────────────────


def test_run_issue_dispatches_implementer_then_reviewer_with_expected_stages(tmp_path):
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    issue = {
        "number": 3,
        "title": "T",
        "body": "body",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert [(call.role, call.prompt.template, call.stage) for call in fake.calls] == [
        (
            AgentRole.IMPLEMENTER,
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            "pre-implementation",
        ),
        (AgentRole.REVIEWER, PromptTemplate.REVIEW, "pre-review"),
    ]


def test_run_issue_cross_service_resolved_service_uses_full_role_prompt_with_interrupted_work_clause(
    tmp_path,
):
    branch = "pycastle/issue-3"
    worktree = worktree_identity(branch, tmp_path).path
    issue = {
        "number": 3,
        "title": "Handoff title",
        "body": "A detailed issue body.",
        "comments": [
            {
                "author": "alice",
                "created_at": "2026-06-01T00:00:00Z",
                "body": "Previous agent noted a follow-up.",
            }
        ],
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    cfg = Config(
        implement_override=StageOverride(service="opencode"),
        review_override=StageOverride(service="opencode"),
    )
    registry = ServiceRegistry(
        {
            "opencode": _CrossServiceTestService("opencode"),
            "codex": _CrossServiceTestService("codex"),
        }
    )
    deps = _make_deps(tmp_path, fake, cfg=cfg, service_registry=registry)
    deps.git_svc.is_working_tree_clean.return_value = False
    deps.git_svc.create_worktree(tmp_path, worktree, branch, "sha-abc")
    _seed_prior_role_session_with_service(
        worktree=worktree,
        role=AgentRole.IMPLEMENTER,
        service_name="codex",
        session_id="codex-legacy-session",
    )

    asyncio.run(run_issue(issue, deps, "sha-abc"))

    implementer_call = fake.calls[0]
    assert (
        "Run `git diff` and `git status` to understand the current state"
        in implementer_call.prompt.scope_args["INTERRUPTED_WORK"]
    )
    assert implementer_call.prompt.scope_args["ISSUE_BODY"] == issue["body"]


def test_run_issue_same_service_resumable_session_keeps_interrupted_work_clause_blank(
    tmp_path,
):
    branch = "pycastle/issue-4"
    worktree = worktree_identity(branch, tmp_path).path
    issue = {
        "number": 4,
        "title": "Same service resume",
        "body": "Issue body.",
        "comments": [],
        "labels": ["behavior-slice"],
    }

    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    cfg = Config(implement_override=StageOverride(service="opencode"))
    registry = ServiceRegistry({"opencode": _CrossServiceTestService("opencode")})
    deps = _make_deps(tmp_path, fake, cfg=cfg, service_registry=registry)
    deps.git_svc.is_working_tree_clean.return_value = False
    deps.git_svc.create_worktree(tmp_path, worktree, branch, "sha-abc")
    _seed_prior_role_session_with_service(
        worktree=worktree,
        role=AgentRole.IMPLEMENTER,
        service_name="opencode",
        session_id="opencode-legacy-session",
    )

    asyncio.run(run_issue(issue, deps, "sha-abc"))

    implementer_call = fake.calls[0]
    assert implementer_call.prompt.scope_args["INTERRUPTED_WORK"] == ""
    assert implementer_call.prompt.send_role_prompt_on_resume is False


def test_run_issue_no_prior_session_starts_with_full_role_prompt_without_interrupted_clause(
    tmp_path,
):
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    issue = {
        "number": 5,
        "title": "Fresh start",
        "body": "Fresh body.",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    asyncio.run(run_issue(issue, deps, "sha-abc"))

    implementer_call = fake.calls[0]
    assert implementer_call.prompt.scope_args["INTERRUPTED_WORK"] == ""
    assert implementer_call.prompt.scope_args["ISSUE_BODY"] == issue["body"]
    assert implementer_call.prompt.scope_args["ISSUE_TITLE"] == issue["title"]


def test_run_issue_reviewer_cross_service_resolved_service_uses_full_role_prompt_with_interrupted_work_clause(
    tmp_path,
):
    branch = "pycastle/issue-6"
    worktree = worktree_identity(branch, tmp_path).path
    issue = {
        "number": 6,
        "title": "Reviewer handoff",
        "body": "Reviewer should keep prior work context.",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([CompletionOutput()])
    cfg = Config(review_override=StageOverride(service="opencode"))
    registry = ServiceRegistry(
        {
            "opencode": _CrossServiceTestService("opencode"),
            "codex": _CrossServiceTestService("codex"),
        }
    )
    deps = _make_deps(tmp_path, fake, cfg=cfg, service_registry=registry)
    deps.git_svc.is_working_tree_clean.return_value = False
    deps.git_svc.create_worktree(tmp_path, worktree, branch, "sha-abc")
    _mark_role_stage_done(worktree, role=AgentRole.IMPLEMENTER)
    _seed_prior_role_session_with_service(
        worktree=worktree,
        role=AgentRole.REVIEWER,
        service_name="codex",
        session_id="codex-review-session",
    )

    asyncio.run(run_issue(issue, deps, "sha-abc"))

    reviewer_call = fake.calls[0]
    assert reviewer_call.role is AgentRole.REVIEWER
    assert (
        "Run `git diff` and `git status` to understand the current state"
        in reviewer_call.prompt.scope_args["INTERRUPTED_WORK"]
    )
    assert reviewer_call.prompt.scope_args["ISSUE_BODY"] == issue["body"]


# ── Issue 349: issue_title threading ─────────────────────────────────────────


def test_run_issue_passes_issue_title_to_implementer(tmp_path):
    issue = {
        "number": 5,
        "title": "Fix auth timeout",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert fake.calls[0].issue_title == "Fix auth timeout"


def test_run_issue_passes_issue_title_to_reviewer(tmp_path):
    issue = {
        "number": 5,
        "title": "Fix auth timeout",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert fake.calls[1].issue_title == "Fix auth timeout"


def test_run_issue_uses_carried_ready_outcome_for_template_and_work_bodies(tmp_path):
    from pycastle.issue_readiness import (
        IssueReadiness,
        IssueReadinessKind,
        ReadyIssueOutcome,
        SliceMode,
        WellFormed,
        WellFormedBody,
    )

    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.REFACTOR, label="refactor-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=None,
        ready=ReadyIssueOutcome(
            display_name="docs",
            template=PromptTemplate.IMPLEMENT_DOCS,
        ),
        kind=IssueReadinessKind.READY_AFK,
    )
    issue = {
        "number": 5,
        "title": "Update ADR",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
        "readiness": readiness,
    }
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert fake.calls[0].prompt.template == PromptTemplate.IMPLEMENT_DOCS
    assert fake.calls[0].work_body == 'implementing docs "Update ADR"'
    assert fake.calls[1].work_body == 'reviewing docs "Update ADR"'


# ── run_issue: worktree lifecycle ─────────────────────────────────────────────


def test_run_issue_creates_two_worktrees_implementer_and_reviewer(tmp_path):
    """run_issue must call create_worktree twice: once for the Implementer, once for the Reviewer."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 10,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert deps.git_svc.create_worktree.call_count == 2


def test_run_issue_removes_worktrees_after_successful_run(tmp_path):
    """run_issue must remove both worktrees when the working tree is clean."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 11,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert deps.git_svc.remove_worktree.call_count == 2


def test_run_issue_preserves_worktree_on_usage_limit(tmp_path):
    """run_issue must clean up the Implementer worktree on a handled usage limit."""

    async def _side_effect(request: RunRequest):
        if "Implement Agent" in request.name:
            raise UsageLimitError(reset_time=None)
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_side_effect)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 12,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    with pytest.raises(UsageLimitError):
        asyncio.run(run_issue(issue, deps, "sha-abc"))

    deps.git_svc.remove_worktree.assert_called_once()


def test_run_issue_preserves_worktree_when_dirty(tmp_path):
    """run_issue must not remove the worktree when the working tree is dirty, but still return the issue."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = False

    issue = {
        "number": 13,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    result = asyncio.run(run_issue(issue, deps, "sha-abc"))

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
    issue = {
        "number": 14,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }

    async def _two_concurrent():
        return await asyncio.gather(
            run_issue(issue, deps, "sha-abc", branch_locks=branch_locks),
            run_issue(issue, deps, "sha-abc", branch_locks=branch_locks),
            return_exceptions=True,
        )

    results = asyncio.run(_two_concurrent())
    errors = [r for r in results if isinstance(r, Exception)]
    assert any(isinstance(e, BranchCollisionError) for e in errors)


def test_run_issue_rejects_missing_slice_mode_before_creating_branch_lock(tmp_path):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue = {
        "number": 25,
        "title": "Malformed",
        "body": "x" * 100,
        "comments": [],
        "labels": [],
    }
    branch_locks: dict[str, asyncio.Lock] = {}

    with pytest.raises(
        RuntimeError,
        match=(
            r"Issue #25 is not implement-ready: missing a ready "
            r"slice-mode selection\."
        ),
    ):
        asyncio.run(run_issue(issue, deps, "sha-abc", branch_locks=branch_locks))

    assert branch_locks == {}
    assert not worktree_identity(branch_for(25), tmp_path).path.exists()


# ── run_issue: role-dir stage-done skip logic ─────────────────────────────────


def _seed_review_stage_done(tmp_path: Path, issue_number: int) -> None:
    """Create a completed reviewer session."""
    wt_path = tmp_path / "pycastle" / ".worktrees" / f"issue-{issue_number}"
    role_session = RoleSession(wt_path, AgentRole.REVIEWER)
    role_session.start_fresh()
    role_session.clear_provider_state_and_signal_completion()


def _seed_implement_stage_done(tmp_path: Path, issue_number: int) -> None:
    """Create a completed implementer session."""
    wt_path = tmp_path / "pycastle" / ".worktrees" / f"issue-{issue_number}"
    role_session = RoleSession(wt_path, AgentRole.IMPLEMENTER)
    role_session.start_fresh()
    role_session.clear_provider_state_and_signal_completion()


def _seed_implement_provider_state_without_done(
    tmp_path: Path, issue_number: int
) -> None:
    """Create provider state side-effects without the stage-done sentinel."""
    wt_path = tmp_path / "pycastle" / ".worktrees" / f"issue-{issue_number}"
    provider_dir = wt_path / ".pycastle-session" / "implementer" / "codex"
    provider_dir.mkdir(parents=True)
    (provider_dir / "auth.json").write_text("{}", encoding="utf-8")


def test_run_issue_review_skip_returns_issue_without_invoking_any_agent(tmp_path):
    """When reviewer stage-done signal is set, run_issue returns the issue without spawning agents."""
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    _seed_review_stage_done(tmp_path, 20)

    issue = {
        "number": 20,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    result = asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert result == issue
    assert fake.calls == []


def test_run_issue_review_skip_creates_no_worktree(tmp_path):
    """When reviewer stage-done signal is set, no worktree is created."""
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    _seed_review_stage_done(tmp_path, 21)

    issue = {
        "number": 21,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    deps.git_svc.create_worktree.assert_not_called()


def test_run_issue_implement_skip_invokes_only_reviewer(tmp_path):
    """When implementer stage-done signal is set, run_issue skips Implementer and runs only Reviewer."""
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    _seed_implement_stage_done(tmp_path, 22)

    issue = {
        "number": 22,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    result = asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert result == issue
    assert len(fake.calls) == 1
    assert "Review Agent" in fake.calls[0].name


def test_run_issue_implement_skip_creates_no_implementer_worktree(tmp_path):
    """When implementer stage-done signal is set, no Implementer worktree is created."""
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    _seed_implement_stage_done(tmp_path, 23)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 23,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert deps.git_svc.create_worktree.call_count == 1
    branch_arg = deps.git_svc.create_worktree.call_args[0][2]
    assert branch_arg == "pycastle/issue-23"


def test_run_issue_no_stage_done_signal_runs_both_agents(tmp_path):
    """When no stage-done signal exists, run_issue runs both Implementer and Reviewer normally."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    issue = {
        "number": 24,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    result = asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert result == issue
    assert len(fake.calls) == 2
    assert "Implement Agent" in fake.calls[0].name
    assert "Review Agent" in fake.calls[1].name


def test_run_issue_provider_state_without_done_runs_implementer(tmp_path):
    """Provider state alone must not be treated as an implement-stage done signal."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    _seed_implement_provider_state_without_done(tmp_path, 25)

    issue = {
        "number": 25,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    result = asyncio.run(run_issue(issue, deps, "sha-abc"))

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
    issue = {
        "number": 25,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }

    with pytest.raises(RuntimeError):
        asyncio.run(run_issue(issue, deps, "sha-abc", branch_locks=branch_locks))

    assert not branch_locks["pycastle/issue-25"].locked()


def test_run_issue_pins_worktree_to_caller_supplied_sha(tmp_path):
    """run_issue must pin the implementer worktree to the SHA supplied by the caller."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 16,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "dead1234"))

    assert deps.git_svc.create_worktree.call_count == 2
    implementer_sha = deps.git_svc.create_worktree.call_args_list[0][0][3]
    assert implementer_sha == "dead1234"


def test_run_issue_reviewer_worktree_uses_no_sha(tmp_path):
    """run_issue must create the Reviewer worktree without a pinned SHA (existing-branch path)."""
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 16,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert deps.git_svc.create_worktree.call_count == 2
    reviewer_sha = deps.git_svc.create_worktree.call_args_list[1][0][3]
    assert reviewer_sha is None


# ── Issue 437: live agent-start progress counter ──────────────────────────────


def test_implement_phase_sets_initial_progress_text(tmp_path):
    """implement_phase registers initial progress text with both counters at 0/Y before any agent runs."""
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([CompletionOutput()] * 4)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    update_phase_calls = [
        c for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    ]
    assert update_phase_calls[0] == (
        "update_phase",
        "Implement",
        "Running: started implement Agents for 0/2 issues · started review Agents for 0/2 issues",
    )


def test_implement_phase_increments_progress_text_per_semaphore_acquisition(tmp_path):
    """implement_phase increments the counter each time a new issue acquires the semaphore."""
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 3,
            "title": "C",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([CompletionOutput()] * 6)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    update_phase_calls = [
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    ]
    assert any(
        "started implement Agents for 0/3 issues" in m for m in update_phase_calls
    )
    assert any(
        "started implement Agents for 1/3 issues" in m for m in update_phase_calls
    )
    assert any(
        "started implement Agents for 2/3 issues" in m for m in update_phase_calls
    )
    assert any(
        "started implement Agents for 3/3 issues" in m for m in update_phase_calls
    )


def test_implement_phase_seeds_initial_progress_from_done_implementers(tmp_path):
    """Resumed issues with completed Implementers start with implement progress already counted."""
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    _seed_implement_stage_done(tmp_path, 1)
    _seed_implement_stage_done(tmp_path, 2)
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    initial = next(
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    )
    assert (
        initial
        == "Running: started implement Agents for 2/2 issues · started review Agents for 0/2 issues"
    )


def test_implement_phase_seeds_initial_progress_from_done_reviewers(tmp_path):
    """Resumed issues with completed Reviewers start with review progress already counted."""
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    _seed_implement_stage_done(tmp_path, 1)
    _seed_review_stage_done(tmp_path, 1)
    fake = FakeAgentRunner([])
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    initial = next(
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    )
    assert (
        initial
        == "Running: started implement Agents for 1/1 issues · started review Agents for 1/1 issues"
    )


def test_implement_phase_mixed_fresh_and_done_implementer_starts_at_seed(tmp_path):
    """A mixed fresh/resumed batch starts implement progress from the resumed issue count."""
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    _seed_implement_stage_done(tmp_path, 1)
    fake = FakeAgentRunner([CompletionOutput()] * 3)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    initial = next(
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    )
    assert (
        initial
        == "Running: started implement Agents for 1/2 issues · started review Agents for 0/2 issues"
    )


def test_implement_phase_progress_total_matches_issue_count(tmp_path):
    """Y in the progress text equals the number of issues passed to implement_phase."""
    issues = [
        {
            "number": i,
            "title": f"Issue {i}",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
        for i in range(1, 6)
    ]
    fake = FakeAgentRunner([CompletionOutput()] * 10)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    initial = next(
        c[2] for c in sd.calls if c[0] == "update_phase" and c[1] == "Implement"
    )
    assert (
        initial
        == "Running: started implement Agents for 0/5 issues · started review Agents for 0/5 issues"
    )


def test_implement_phase_counter_is_monotonic(tmp_path):
    """Counter in progress text only increases and never decrements."""
    issues = [
        {
            "number": i,
            "title": f"Issue {i}",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
        for i in range(1, 4)
    ]
    fake = FakeAgentRunner([CompletionOutput()] * 6)
    sd = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, fake, status_display=sd)

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    import re

    implement_counts = []
    review_counts = []
    for c in sd.calls:
        if c[0] != "update_phase" or c[1] != "Implement":
            continue
        m = re.search(r"started implement Agents for (\d+)/", c[2])
        if m:
            implement_counts.append(int(m.group(1)))
        m = re.search(r"started review Agents for (\d+)/", c[2])
        if m:
            review_counts.append(int(m.group(1)))
    assert implement_counts == sorted(implement_counts)
    assert review_counts == sorted(review_counts)


def test_run_issue_calls_on_started_for_implement_and_review(tmp_path):
    """run_issue calls on_started once for implement and once for review."""
    fired: list[str] = []
    fake = FakeAgentRunner([CompletionOutput()] * 2)
    deps = _make_deps(tmp_path, fake)

    issue = {
        "number": 1,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(
        run_issue(issue, deps, "sha-abc", on_started=lambda role: fired.append(role))
    )

    assert fired == ["implement", "review"]


def test_run_issue_on_started_not_called_when_review_already_done(tmp_path):
    """run_issue does not call on_started when reviewer stage-done signal is set."""
    fired: list[str] = []
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake)
    _seed_review_stage_done(tmp_path, 1)

    issue = {
        "number": 1,
        "title": "Fix thing",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(
        run_issue(issue, deps, "sha-abc", on_started=lambda role: fired.append(role))
    )

    assert fired == []


# ── run_issue: commit wiring ─────────────────────────────────────────────────


def test_run_issue_commits_implementer_with_issue_number_and_message(tmp_path):
    """After Implementer returns CommitMessageOutput with message, commit uses 'Implement #N - <msg>'."""
    fake = FakeAgentRunner(
        [CommitMessageOutput(message="add foo"), CommitMessageOutput(message="tidy")]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 40,
        "title": "Fix",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    impl_call = deps.git_svc.commit.call_args_list[0]
    assert impl_call[0][2] == "Implement #40 - add foo"


def test_run_issue_commits_implementer_with_title_when_no_commit_message_tag(tmp_path):
    """After Implementer returns CommitMessageOutput(message=None), commit uses issue title as fallback."""
    fake = FakeAgentRunner(
        [CommitMessageOutput(message=None), CommitMessageOutput(message=None)]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 43,
        "title": "Fix the login bug",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    impl_call = deps.git_svc.commit.call_args_list[0]
    assert impl_call[0][2] == "Implement #43 - Fix the login bug"


def test_run_issue_commits_reviewer_with_issue_number_and_message(tmp_path):
    """After Reviewer returns CommitMessageOutput with message, commit uses 'Review #N - <msg>'."""
    fake = FakeAgentRunner(
        [
            CommitMessageOutput(message="add foo"),
            _reviewer_output("rename var"),
        ]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 41,
        "title": "Fix",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    review_call = deps.git_svc.commit.call_args_list[1]
    assert review_call[0][2] == "Review #41 - rename var"


def test_run_issue_commits_reviewer_with_title_when_no_commit_message_tag(tmp_path):
    """After Reviewer returns CommitMessageOutput(message=None), commit uses issue title as fallback."""
    fake = FakeAgentRunner([CommitMessageOutput(message=None), _reviewer_output(None)])
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = True

    issue = {
        "number": 44,
        "title": "Add dark mode",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    review_call = deps.git_svc.commit.call_args_list[1]
    assert review_call[0][2] == "Review #44 - Add dark mode"


def test_run_issue_on_started_fires_when_only_reviewer_runs(tmp_path):
    """run_issue calls on_started for review when implement stage-done signal is set."""
    fired: list[str] = []
    fake = FakeAgentRunner([CompletionOutput()])
    deps = _make_deps(tmp_path, fake)
    _seed_implement_stage_done(tmp_path, 1)

    issue = {
        "number": 1,
        "title": "Fix auth",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(
        run_issue(issue, deps, "sha-abc", on_started=lambda role: fired.append(role))
    )

    assert fired == ["review"]


# ── run_issue: role session cleanup after commit ──────────────────────────────


def test_run_issue_clears_implementer_session_dir_contents_after_commit(tmp_path):
    """After Implementer commits, session dir is cleared (not deleted), leaving the stage-done signal.

    The worktree is made dirty so it is preserved, making the session dir observable.
    """
    fake = FakeAgentRunner(
        [CommitMessageOutput(message="fix it"), _reviewer_output("tidy")]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = False  # preserve worktree

    wt_name = "issue-50"
    wt_path = tmp_path / "pycastle" / ".worktrees" / wt_name
    impl_session_dir = wt_path / ".pycastle-session" / "implementer"

    original_create = deps.git_svc.create_worktree.side_effect

    def _seeding_create(repo, path, branch, sha=None):
        original_create(repo, path, branch, sha)
        if not impl_session_dir.is_dir():
            impl_session_dir.mkdir(parents=True, exist_ok=True)
            (impl_session_dir / "session.json").write_text("{}")

    deps.git_svc.create_worktree.side_effect = _seeding_create

    issue = {
        "number": 50,
        "title": "Fix",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    # Dir exists (not removed) and only the explicit done sentinel remains.
    assert impl_session_dir.is_dir()
    assert sorted(path.name for path in impl_session_dir.iterdir()) == ["_done"]


def test_run_issue_clears_reviewer_session_dir_contents_after_commit(tmp_path):
    """After Reviewer commits, session dir is cleared (not deleted), leaving the stage-done signal.

    The worktree is made dirty so it is preserved, making the session dir observable.
    """
    fake = FakeAgentRunner(
        [CommitMessageOutput(message="fix it"), _reviewer_output("tidy")]
    )
    deps = _make_deps(tmp_path, fake)
    deps.git_svc.is_working_tree_clean.return_value = False  # preserve worktree

    wt_name = "issue-51"
    wt_path = tmp_path / "pycastle" / ".worktrees" / wt_name
    rev_session_dir = wt_path / ".pycastle-session" / "reviewer"

    original_create = deps.git_svc.create_worktree.side_effect

    def _seeding_create(repo, path, branch, sha=None):
        original_create(repo, path, branch, sha)
        if not rev_session_dir.is_dir():
            rev_session_dir.mkdir(parents=True, exist_ok=True)
            (rev_session_dir / "session.json").write_text("{}")

    deps.git_svc.create_worktree.side_effect = _seeding_create

    issue = {
        "number": 51,
        "title": "Fix",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    asyncio.run(run_issue(issue, deps, "sha-abc"))

    assert rev_session_dir.is_dir()
    assert sorted(path.name for path in rev_session_dir.iterdir()) == ["_done"]


# ── Issue 886: concurrency bounds ─────────────────────────────────────────────


def test_implement_phase_never_runs_more_than_max_parallel_agents_at_once(tmp_path):
    """At no moment do more than max_parallel Implementer/Reviewer agents run concurrently."""
    max_parallel = 3
    issues = [
        {
            "number": i,
            "title": f"Issue {i}",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
        for i in range(1, 8)
    ]

    active: list[int] = [0]
    peak: list[int] = [0]
    lock = asyncio.Lock()

    async def _agent(request: RunRequest):
        async with lock:
            active[0] += 1
            if active[0] > peak[0]:
                peak[0] = active[0]
        await asyncio.sleep(0)
        async with lock:
            active[0] -= 1
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_agent)
    deps = _make_deps(tmp_path, fake, status_display=PlainStatusDisplay())
    deps = dataclasses.replace(
        deps, cfg=Config(max_parallel=max_parallel, max_iterations=1)
    )

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert peak[0] <= max_parallel, (
        f"Peak concurrent agents {peak[0]} exceeded max_parallel={max_parallel}"
    )


def test_implement_phase_never_opens_more_than_max_parallel_plus_one_worktrees(
    tmp_path,
):
    """At no moment are more than max_parallel + 1 worktrees open concurrently."""
    import shutil

    max_parallel = 3
    issues = [
        {
            "number": i,
            "title": f"Issue {i}",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
        for i in range(1, 8)
    ]

    open_wts: list[int] = [0]
    peak_wts: list[int] = [0]

    git_svc = MagicMock(spec=GitService)
    git_svc.verify_ref_exists.return_value = False

    _registered: list[Path] = []

    def _sync_create(repo, path, branch, sha=None):
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n")
        _registered.append(path)
        open_wts[0] += 1
        if open_wts[0] > peak_wts[0]:
            peak_wts[0] = open_wts[0]

    def _sync_remove(repo, path):
        shutil.rmtree(path, ignore_errors=True)
        _registered[:] = [p for p in _registered if p != path]
        open_wts[0] -= 1

    git_svc.list_worktrees.side_effect = lambda repo: list(_registered)
    git_svc.create_worktree.side_effect = _sync_create
    git_svc.remove_worktree.side_effect = _sync_remove

    async def _agent(request: RunRequest):
        await asyncio.sleep(0)
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_agent)
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        github_svc=MagicMock(spec=GithubService),
        cfg=Config(max_parallel=max_parallel, max_iterations=1),
        logger=RecordingLogger(),
        status_display=PlainStatusDisplay(),
    )

    asyncio.run(implement_phase(issues, deps, "sha-abc"))

    assert peak_wts[0] <= max_parallel + 1, (
        f"Peak open worktrees {peak_wts[0]} exceeded max_parallel+1={max_parallel + 1}"
    )
