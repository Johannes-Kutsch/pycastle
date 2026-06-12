from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ...agents.output_protocol import AgentRole
from .._provider_session_decision import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RecoveredSessionIdPersistence,
)
from .._provider_session_plan import (
    ProviderRunStatePlan,
    ProviderRunStatePlanRequest,
    ProviderSessionDecision,
    plan_provider_run_state,
    record_observed_provider_session_id,
    record_successful_provider_session_metadata,
)
from ..resume import RoleSession, RunKind

if TYPE_CHECKING:
    from ...services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class RunSessionPlanRequest:
    role: AgentRole
    worktree: Path
    namespace: str
    service: AgentService


@dataclasses.dataclass(frozen=True)
class RunSessionPlan:
    role: AgentRole
    worktree: Path
    namespace: str
    service: AgentService
    run_kind: RunKind
    service_state_dir: Path | None
    provider_state_dir_relpath: str | None
    host_provider_state_dir: Path | None
    provider_session_id: str | None
    auth_seeding_requirement: AuthSeedingRequirement
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    provider_session_plan: ProviderSessionDecision | None = None
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False
    _provider_run_state_plan: ProviderRunStatePlan | None = dataclasses.field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        provider_run_state_plan = self._provider_run_state_plan
        if provider_run_state_plan is None:
            provider_run_state_plan = ProviderRunStatePlan(
                role_session=RoleSession(self.worktree, self.role, self.namespace),
                service_name=self.service.name,
                run_kind=self.run_kind,
                provider_state_dir=self.host_provider_state_dir,
                provider_state_dir_relpath=self.provider_state_dir_relpath,
                provider_session_id=self.provider_session_id,
                auth_seeding_requirement=self.auth_seeding_requirement,
                recovered_session_id_persistence=(
                    self.recovered_session_id_persistence
                ),
                service_state_dir=self.service_state_dir,
                exact_transcript_match=self.exact_transcript_match,
                auth_seed_action=self.auth_seed_action,
            )
            object.__setattr__(
                self,
                "_provider_run_state_plan",
                provider_run_state_plan,
            )
        if self.provider_session_plan is None:
            object.__setattr__(
                self,
                "provider_session_plan",
                provider_run_state_plan.provider_session_decision(),
            )

    def provider_state_dir_container_path(self, container_workspace: str) -> str | None:
        provider_run_state_plan = self._provider_run_state_plan
        if provider_run_state_plan is None:
            return None
        return provider_run_state_plan.provider_state_dir_container_path(
            worktree=self.worktree,
            container_workspace=container_workspace,
        )

    def prepared_provider_session_id(self) -> str | None:
        provider_run_state_plan = self._provider_run_state_plan
        if provider_run_state_plan is None:
            return None
        provider_session_id = provider_run_state_plan.prepared_provider_session_id()
        object.__setattr__(self, "provider_session_id", provider_session_id)
        return provider_session_id

    def prepare_host_provider_state_dir(self) -> None:
        provider_run_state_plan = self._provider_run_state_plan
        if provider_run_state_plan is None:
            return
        provider_run_state_plan.prepare_provider_state_dir()

    def capture_provider_session_id(self, provider_session_id: str) -> None:
        object.__setattr__(self, "provider_session_id", provider_session_id)
        provider_run_state_plan = self._provider_run_state_plan
        if provider_run_state_plan is not None:
            provider_run_state_plan.remember_provider_session_id(provider_session_id)
            return
        record_observed_provider_session_id(
            worktree=self.worktree,
            role=self.role,
            namespace=self.namespace,
            service_name=self.service.name,
            service_state_dir=self.service_state_dir,
            provider_session_id=provider_session_id,
        )

    def record_successful_run(self, provider_session_id: str | None = None) -> None:
        session_id = provider_session_id or self.provider_session_id
        if provider_session_id is not None:
            self.capture_provider_session_id(provider_session_id)
        record_successful_provider_session_metadata(
            worktree=self.worktree,
            role=self.role,
            namespace=self.namespace,
            service_name=self.service.name,
            provider_session_id=session_id,
        )

    @classmethod
    def for_service(
        cls,
        *,
        role: AgentRole,
        worktree: Path,
        namespace: str,
        service: AgentService,
    ) -> RunSessionPlan:
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
        provider_session_plan=provider_run_state_plan.provider_session_decision(),
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
