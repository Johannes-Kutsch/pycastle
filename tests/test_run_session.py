from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pycastle.agents.output_protocol import AgentRole
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    RecoveredSessionIdPersistence,
    RunSessionPlan,
)
from pycastle.services.agent_service import AgentService


@dataclass
class _FakeAgentService:
    relpath: str | None
    name: str = "fake"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return self.relpath


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
