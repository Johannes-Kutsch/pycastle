from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pycastle_agent_runtime.session_planning import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RecoveredSessionIdPersistence,
    ResidentSessionPlan,
)

from ...agents.output_protocol import AgentRole
from .._provider_session_plan import (
    ProviderRunStatePlanRequest,
    plan_provider_run_state,
)

if TYPE_CHECKING:
    from ...services.agent_service import AgentService


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
    provider_run_state_plan = plan_provider_run_state(
        ProviderRunStatePlanRequest(
            worktree=request.worktree,
            role=request.role,
            namespace=request.namespace,
            service=request.service,
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


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "RecoveredSessionIdPersistence",
    "RunSessionPlan",
    "RunSessionPlanRequest",
    "plan_run_session",
]
