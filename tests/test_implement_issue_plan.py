from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.config import Config, StageOverride
from pycastle.iteration.implement_issue_plan import (
    plan_issue_execution,
    plan_issue_execution_from_worktree,
    plan_ready_issue_slice,
)
from pycastle.issue_readiness import (
    IssueReadiness,
    IssueReadinessKind,
    ReadyIssueOutcome,
    SliceMode,
    WellFormed,
    WellFormedBody,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import GitService, ServiceRegistry
from pycastle.session import RoleSession
from pycastle.session.service_session_store import save_service_session_metadata
from tests.support import FakeAgentRunner, _make_deps


class _CrossServiceTestService:
    def __init__(self, name: str) -> None:
        self.name = name

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        relpath = f".pycastle-session/{role.value}/{self.name}"
        return f"{relpath}/{namespace}" if namespace else relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(state_dir.iterdir())


def _issue() -> dict:
    return {
        "number": 1909,
        "title": "Scaffold implement issue execution planning module",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }


def _managed_issue_mount(repo_root: Path, name: str) -> Path:
    mount_path = repo_root / "pycastle" / ".worktrees" / name
    mount_path.mkdir(parents=True, exist_ok=True)
    return mount_path


def _issue_worktree(repo_root: Path, issue_number: int) -> Path:
    return repo_root / "pycastle" / ".worktrees" / f"issue-{issue_number}"


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


def test_plan_issue_execution_returns_run_steps_for_ready_issue(tmp_path):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
        implement_done=False,
        review_done=False,
    )

    assert plan.issue_number == 1909
    assert plan.issue_title == "Scaffold implement issue execution planning module"
    assert plan.branch == "pycastle/issue-1909"
    assert plan.planner_sha == "sha-abc"
    assert plan.slice_mode_display_name == "behavior"

    assert plan.implementer_step.outcome == "run"
    assert plan.implementer_step.role_name == "implementer"
    assert plan.implementer_step.stage == "pre-implementation"
    assert (
        plan.implementer_step.work_body
        == 'implementing behavior "Scaffold implement issue execution planning module"'
    )
    assert plan.implementer_step.prompt_template == PromptTemplate.IMPLEMENT_BEHAVIOR
    assert plan.implementer_step.prompt_scope_args["ISSUE_NUMBER"] == "1909"
    assert plan.implementer_step.prompt_scope_args["BRANCH"] == "pycastle/issue-1909"
    assert plan.implementer_step.prompt_scope_args["INTERRUPTED_WORK"] == ""
    assert plan.implementer_step.mount_setup_failure is None
    assert (
        plan.implementer_step.commit_fallback_subject.commit_prefix
        == "Implement #1909 - "
    )
    assert (
        plan.implementer_step.commit_fallback_subject.fallback_subject
        == "Scaffold implement issue execution planning module"
    )

    assert plan.reviewer_step.outcome == "run"
    assert plan.reviewer_step.role_name == "reviewer"
    assert plan.reviewer_step.stage == "pre-review"
    assert (
        plan.reviewer_step.work_body
        == 'reviewing behavior "Scaffold implement issue execution planning module"'
    )
    assert plan.reviewer_step.prompt_template == PromptTemplate.REVIEW
    assert plan.reviewer_step.prompt_scope_args["BRANCH"] == "pycastle/issue-1909"
    assert plan.reviewer_step.prompt_scope_args["INTERRUPTED_WORK"] == ""
    assert plan.reviewer_step.mount_setup_failure is None
    assert plan.reviewer_step.commit_fallback_subject.commit_prefix == "Review #1909 - "


def test_plan_issue_execution_skips_both_steps_when_review_stage_done_signal_exists(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
        implement_done=False,
        review_done=True,
    )

    assert plan.implementer_step.outcome == "skip"
    assert plan.implementer_step.skip_reason == "review stage already complete"
    assert plan.reviewer_step.outcome == "skip"
    assert plan.reviewer_step.skip_reason == "review stage already complete"


def test_plan_issue_execution_marks_issue_complete_when_review_stage_done_signal_exists(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
        implement_done=False,
        review_done=True,
    )

    assert plan.issue_outcome == "complete"


def test_plan_issue_execution_from_worktree_skips_implementer_when_only_implement_stage_done(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue_worktree = _issue_worktree(tmp_path, 1909)
    RoleSession(issue_worktree, AgentRole.IMPLEMENTER).start_fresh()
    RoleSession(
        issue_worktree, AgentRole.IMPLEMENTER
    ).clear_provider_state_and_signal_completion()
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution_from_worktree(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        worktree_path=issue_worktree,
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
    )

    assert plan.issue_outcome == "incomplete"
    assert plan.implementer_step.outcome == "skip"
    assert plan.implementer_step.skip_reason == "implement stage already complete"
    assert plan.reviewer_step.outcome == "run"


def test_plan_issue_execution_from_worktree_marks_issue_complete_when_review_stage_done_signal_exists(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue_worktree = _issue_worktree(tmp_path, 1909)
    RoleSession(issue_worktree, AgentRole.REVIEWER).start_fresh()
    RoleSession(
        issue_worktree, AgentRole.REVIEWER
    ).clear_provider_state_and_signal_completion()
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution_from_worktree(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        worktree_path=issue_worktree,
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
    )

    assert plan.issue_outcome == "complete"
    assert plan.implementer_step.outcome == "skip"
    assert plan.reviewer_step.outcome == "skip"
    assert plan.run_steps == ()


def test_plan_issue_execution_from_worktree_orders_run_steps_when_no_stage_done_signal_exists(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue_worktree = _issue_worktree(tmp_path, 1909)
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution_from_worktree(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        worktree_path=issue_worktree,
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
    )

    assert plan.issue_outcome == "incomplete"
    assert [(step.role_name, step.outcome) for step in plan.steps] == [
        ("implementer", "run"),
        ("reviewer", "run"),
    ]


def test_plan_issue_execution_from_worktree_keeps_provider_state_without_done_runnable(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue_worktree = _issue_worktree(tmp_path, 1909)
    provider_dir = issue_worktree / ".pycastle-session" / "implementer" / "codex"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "auth.json").write_text("{}", encoding="utf-8")
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution_from_worktree(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        worktree_path=issue_worktree,
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
    )

    assert [step.role_name for step in plan.run_steps] == [
        "implementer",
        "reviewer",
    ]


def test_plan_issue_execution_from_worktree_keeps_reviewer_provider_state_without_done_runnable(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue_worktree = _issue_worktree(tmp_path, 1909)
    RoleSession(issue_worktree, AgentRole.IMPLEMENTER).start_fresh()
    RoleSession(
        issue_worktree, AgentRole.IMPLEMENTER
    ).clear_provider_state_and_signal_completion()
    provider_dir = issue_worktree / ".pycastle-session" / "reviewer" / "codex"
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "auth.json").write_text("{}", encoding="utf-8")
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution_from_worktree(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        worktree_path=issue_worktree,
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
    )

    assert plan.issue_outcome == "incomplete"
    assert [step.role_name for step in plan.run_steps] == ["reviewer"]


def test_plan_issue_execution_reports_mount_setup_failure_for_invalid_managed_worktree_mount(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    _managed_issue_mount(tmp_path, "issue-1909-review")
    implement_mount_path = tmp_path / "outside-worktree"
    implement_mount_path.mkdir()
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")

    plan = plan_issue_execution(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
        implement_done=False,
        review_done=False,
    )

    assert plan.implementer_step.outcome == "setup_failure"
    assert plan.implementer_step.mount_setup_failure is not None
    assert (
        plan.implementer_step.mount_setup_failure.rejection.rejection_code
        == "invalid_mount_path"
    )
    assert "managed worktree mount" in (
        plan.implementer_step.mount_setup_failure.error_message
    )
    assert plan.reviewer_step.outcome == "run"


def test_plan_issue_execution_raises_not_implement_ready_before_mount_checks_for_missing_slice_mode(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue = {
        **_issue(),
        "labels": [],
        "body": "x" * 100,
    }
    implement_mount_path = tmp_path / "outside-implement"
    implement_mount_path.mkdir()
    review_mount_path = tmp_path / "outside-review"
    review_mount_path.mkdir()

    with pytest.raises(
        RuntimeError,
        match=(
            r"Issue #1909 is not implement-ready: missing a ready "
            r"slice-mode selection\."
        ),
    ):
        plan_issue_execution(
            issue=issue,
            deps=deps,
            sha="sha-abc",
            implement_mount_path=implement_mount_path,
            review_mount_path=review_mount_path,
            implement_done=False,
            review_done=False,
        )


def test_plan_issue_execution_raises_not_implement_ready_before_mount_checks_for_multiple_slice_modes(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    issue = {
        **_issue(),
        "labels": ["behavior-slice", "docs-slice"],
        "body": "x" * 100,
    }
    implement_mount_path = tmp_path / "outside-implement"
    implement_mount_path.mkdir()
    review_mount_path = tmp_path / "outside-review"
    review_mount_path.mkdir()

    with pytest.raises(
        RuntimeError,
        match=(
            r"Issue #1909 is not implement-ready: missing a ready "
            r"slice-mode selection\."
        ),
    ):
        plan_issue_execution(
            issue=issue,
            deps=deps,
            sha="sha-abc",
            implement_mount_path=implement_mount_path,
            review_mount_path=review_mount_path,
            implement_done=False,
            review_done=False,
        )


def test_plan_issue_execution_marks_interrupted_work_for_cross_service_dirty_role_session(
    tmp_path,
):
    git_svc = MagicMock(spec=GitService)
    git_svc.is_working_tree_clean.return_value = False
    cfg = Config(implement_override=StageOverride(service="codex"))
    registry = ServiceRegistry(
        {
            "codex": _CrossServiceTestService("codex"),
            "opencode": _CrossServiceTestService("opencode"),
        }
    )
    deps = _make_deps(
        tmp_path,
        FakeAgentRunner([]),
        cfg=cfg,
        git_svc=git_svc,
        service_registry=registry,
    )
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")
    _seed_prior_role_session_with_service(
        implement_mount_path,
        role=AgentRole.IMPLEMENTER,
        service_name="opencode",
        session_id="session-1",
    )
    (implement_mount_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    plan = plan_issue_execution(
        issue=_issue(),
        deps=deps,
        sha="sha-abc",
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
        implement_done=False,
        review_done=False,
    )

    assert (
        "This worktree has uncommitted changes from a previous agent run."
        in (plan.implementer_step.prompt_scope_args["INTERRUPTED_WORK"])
    )
    assert plan.reviewer_step.prompt_scope_args["INTERRUPTED_WORK"] == ""


def test_plan_issue_execution_uses_carried_ready_slice_outcome_for_template_and_display(
    tmp_path,
):
    deps = _make_deps(tmp_path, FakeAgentRunner([]))
    implement_mount_path = _managed_issue_mount(tmp_path, "issue-1909-implement")
    review_mount_path = _managed_issue_mount(tmp_path, "issue-1909-review")
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
        **_issue(),
        "labels": ["behavior-slice"],
        "readiness": readiness,
    }

    plan = plan_issue_execution(
        issue=issue,
        deps=deps,
        sha="sha-abc",
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
        implement_done=False,
        review_done=False,
    )

    assert plan.slice_mode_display_name == "docs"
    assert plan.implementer_step.prompt_template == PromptTemplate.IMPLEMENT_DOCS
    assert (
        plan.implementer_step.work_body
        == 'implementing docs "Scaffold implement issue execution planning module"'
    )
    assert (
        plan.reviewer_step.work_body
        == 'reviewing docs "Scaffold implement issue execution planning module"'
    )


def test_plan_ready_issue_slice_returns_prompt_and_shared_display_bodies_from_carried_readiness():
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
        **_issue(),
        "labels": ["behavior-slice"],
        "readiness": readiness,
    }

    ready_slice = plan_ready_issue_slice(issue, Config())

    assert ready_slice.display_name == "docs"
    assert ready_slice.implement_prompt_template == PromptTemplate.IMPLEMENT_DOCS
    assert (
        ready_slice.implement_work_body
        == 'implementing docs "Scaffold implement issue execution planning module"'
    )
    assert (
        ready_slice.review_work_body
        == 'reviewing docs "Scaffold implement issue execution planning module"'
    )
