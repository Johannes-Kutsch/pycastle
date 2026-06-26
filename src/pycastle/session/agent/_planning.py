from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pycastle.session_planning import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    ProviderRunStatePlan,
    ProviderRunStatePlanRequest,
    RecoveredSessionIdPersistence,
    ResidentSessionPlan,
    plan_provider_run_state,
)
from pycastle.provider_session_adapter import provider_session_adapter_for_service

from ...agents.output_protocol import AgentRole
from ..role import RoleSession
from ..service_session_store import store_for_role_session

if TYPE_CHECKING:
    from ...services.runtime_services import AgentService


@dataclasses.dataclass(frozen=True)
class RunSessionPlanRequest:
    role: AgentRole
    worktree: Path
    namespace: str
    service: "AgentService"


@dataclasses.dataclass(frozen=True)
class RunSessionPlan(ResidentSessionPlan):
    service: "AgentService"
    auth_seed_action: LocalAuthSeedAction | None = None
    recovered_session_id_persistence: RecoveredSessionIdPersistence = (
        RecoveredSessionIdPersistence.SKIP
    )

    def prepare_host_provider_state_dir(self) -> None:
        self.prepare_provider_state_dir()

    @classmethod
    def for_service(
        cls,
        *,
        role: AgentRole,
        worktree: Path,
        namespace: str,
        service: "AgentService",
    ) -> "RunSessionPlan":
        return plan_run_session(
            RunSessionPlanRequest(
                role=role,
                worktree=worktree,
                namespace=namespace,
                service=service,
            )
        )


def plan_run_session(request: RunSessionPlanRequest) -> RunSessionPlan:
    role_session = RoleSession(request.worktree, request.role, request.namespace)
    provider_run_state_plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=request.worktree,
            role=request.role,
            namespace=request.namespace,
            service=request.service,
            role_session=store_for_role_session(role_session),
            provider_session_adapter=provider_session_adapter_for_service(
                request.service
            ),
        )
    )
    return RunSessionPlan(
        role=request.role,
        worktree=request.worktree,
        namespace=request.namespace,
        service=request.service,
        run_kind=provider_run_state_plan.run_kind,
        service_state_dir=provider_run_state_plan.service_state_dir,
        provider_state_dir_relpath=provider_run_state_plan.provider_state_dir_relpath,
        host_provider_state_dir=provider_run_state_plan.provider_state_dir,
        provider_session_id=provider_run_state_plan.provider_session_id,
        auth_seeding_requirement=provider_run_state_plan.auth_seeding_requirement,
        recovered_session_id_persistence=(
            provider_run_state_plan.recovered_session_id_persistence
        ),
        auth_seed_action=cast(
            LocalAuthSeedAction | None,
            provider_run_state_plan.auth_seed_action,
        ),
        exact_transcript_match=provider_run_state_plan.exact_transcript_match,
        use_service_state_dir_for_container=(
            provider_run_state_plan.use_service_state_dir_for_container
        ),
        _provider_run_state_plan=provider_run_state_plan,
    )


def run_session_plan_from_provider_run_state_plan(
    *,
    role: AgentRole,
    worktree: Path,
    namespace: str,
    service: "AgentService",
    provider_run_state_plan: ProviderRunStatePlan,
) -> RunSessionPlan:
    return RunSessionPlan(
        role=role,
        worktree=worktree,
        namespace=namespace,
        service=service,
        run_kind=provider_run_state_plan.run_kind,
        service_state_dir=provider_run_state_plan.service_state_dir,
        provider_state_dir_relpath=provider_run_state_plan.provider_state_dir_relpath,
        host_provider_state_dir=provider_run_state_plan.provider_state_dir,
        provider_session_id=provider_run_state_plan.provider_session_id,
        auth_seeding_requirement=provider_run_state_plan.auth_seeding_requirement,
        recovered_session_id_persistence=(
            provider_run_state_plan.recovered_session_id_persistence
        ),
        auth_seed_action=cast(
            LocalAuthSeedAction | None,
            provider_run_state_plan.auth_seed_action,
        ),
        exact_transcript_match=provider_run_state_plan.exact_transcript_match,
        use_service_state_dir_for_container=(
            provider_run_state_plan.use_service_state_dir_for_container
        ),
        _provider_run_state_plan=provider_run_state_plan,
    )


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "RecoveredSessionIdPersistence",
    "RunSessionPlan",
    "RunSessionPlanRequest",
    "plan_run_session",
    "run_session_plan_from_provider_run_state_plan",
]
