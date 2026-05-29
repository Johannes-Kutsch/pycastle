from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pycastle.agents.output_protocol import AgentRole
from pycastle.services import ClaudeService
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    RecoveredSessionIdPersistence,
    RunSessionPlan,
)
from pycastle.session import RoleSession, RunKind
from pycastle.services.agent_service import AgentService


@dataclass
class _FakeAgentService:
    relpath: str | None
    name: str = "fake"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return False


def test_run_session_plan_uses_service_state_dir_for_namespaced_role(tmp_path: Path):
    service = cast(
        AgentService, _FakeAgentService(".pycastle-session/improve/main/codex/")
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
        provider_session_id="thread-123",
        auth_seeding_requirement=AuthSeedingRequirement.REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.PERSIST,
    )

    assert plan == RunSessionPlan(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
        run_kind=RunKind.FRESH,
        service_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_session_id="thread-123",
        auth_seeding_requirement=AuthSeedingRequirement.REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.PERSIST,
    )


def test_run_session_plan_keeps_none_service_state_dir_when_service_has_no_state_dir(
    tmp_path: Path,
):
    service = cast(AgentService, _FakeAgentService(None))

    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=service,
        provider_session_id=None,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )

    assert plan.service_state_dir is None
    assert plan.provider_session_id is None
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.SKIP


def test_run_session_plan_reports_fresh_for_claude_with_absent_or_empty_state_dir(
    tmp_path: Path,
):
    service = ClaudeService()
    expected_state_dir = tmp_path / ".pycastle-session/implementer/claude"
    expected_session_id = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()

    absent_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    expected_state_dir.mkdir(parents=True)

    empty_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert absent_plan.run_kind is RunKind.FRESH
    assert empty_plan.run_kind is RunKind.FRESH
    assert absent_plan.service_state_dir == expected_state_dir
    assert empty_plan.service_state_dir == expected_state_dir
    assert absent_plan.provider_session_id == expected_session_id
    assert empty_plan.provider_session_id == expected_session_id


def test_run_session_plan_reports_resume_for_claude_with_populated_state_dir(
    tmp_path: Path,
):
    service = ClaudeService()
    expected_state_dir = tmp_path / ".pycastle-session/implementer/claude"
    expected_session_id = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
    expected_state_dir.mkdir(parents=True)
    (expected_state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.service_state_dir == expected_state_dir
    assert plan.provider_session_id == expected_session_id


def test_run_session_plan_namespaces_claude_provider_session_identity_only_when_non_empty(
    tmp_path: Path,
):
    service = ClaudeService()

    main_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    issues_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="issues",
        service=service,
    )
    base_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert main_plan.provider_session_id != issues_plan.provider_session_id
    assert main_plan.service_state_dir != issues_plan.service_state_dir
    assert (
        base_plan.provider_session_id
        == RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
    )
    assert (
        base_plan.provider_session_id
        == RoleSession(tmp_path, AgentRole.IMPLEMENTER, "").session_uuid()
    )
