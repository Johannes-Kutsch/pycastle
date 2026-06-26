from __future__ import annotations

import dataclasses
from pathlib import Path

from pycastle.provider_session_adapter import (
    ProviderSessionAdapter,
    provider_session_adapter_for_service,
    provider_session_adapter_for_service_name,
)
from pycastle.session_planning import (
    AuthSeedingRequirement,
    ProviderRunStatePlan,
    ProviderRunStatePlanRequest as RuntimeProviderRunStatePlanRequest,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
    plan_provider_run_state as runtime_plan_provider_run_state,
    plan_provider_session as runtime_plan_provider_session,
    record_observed_provider_session_id as runtime_record_observed_provider_session_id,
    record_successful_provider_session_metadata as runtime_record_successful_provider_session_metadata,
)

from ..agents.output_protocol import AgentRole
from ..services.agent_service import AgentService
from .role import RoleSession
from .service_session_store import store_for_role_session


@dataclasses.dataclass(frozen=True)
class ProviderRunStatePlanRequest:
    worktree: Path
    role: AgentRole
    namespace: str
    service: AgentService


ProviderSessionPlanRequest = ProviderRunStatePlanRequest


def _runtime_request(
    request: ProviderRunStatePlanRequest,
) -> RuntimeProviderRunStatePlanRequest:
    role_session = RoleSession(request.worktree, request.role, request.namespace)
    return RuntimeProviderRunStatePlanRequest(
        worktree=request.worktree,
        role=request.role,
        namespace=request.namespace,
        service=request.service,
        role_session=store_for_role_session(role_session),
        provider_session_adapter=provider_session_adapter_for_service(request.service),
    )


def record_observed_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    del service_state_dir
    runtime_record_observed_provider_session_id(
        provider_run_state_plan=ProviderRunStatePlan(
            role_session=store_for_role_session(RoleSession(worktree, role, namespace)),
            provider_session_adapter=provider_session_adapter_for_service_name(
                service_name
            ),
            service_name=service_name,
            run_kind=RoleSession(worktree, role, namespace).run_kind(),
            provider_state_dir=None,
            provider_state_dir_relpath=None,
            provider_session_id=None,
            auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
            recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
        ),
        provider_session_id=provider_session_id,
    )


def record_successful_provider_session_metadata(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    provider_session_id: str | None,
) -> None:
    runtime_record_successful_provider_session_metadata(
        provider_run_state_plan=ProviderRunStatePlan(
            role_session=store_for_role_session(RoleSession(worktree, role, namespace)),
            provider_session_adapter=provider_session_adapter_for_service_name(
                service_name
            ),
            service_name=service_name,
            run_kind=RoleSession(worktree, role, namespace).run_kind(),
            provider_state_dir=None,
            provider_state_dir_relpath=None,
            provider_session_id=provider_session_id,
            auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
            recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
        ),
        provider_session_id=provider_session_id,
    )


def plan_provider_session(
    request: ProviderRunStatePlanRequest,
) -> ProviderSessionDecision:
    return runtime_plan_provider_session(_runtime_request(request))


def plan_provider_run_state(
    request: ProviderRunStatePlanRequest,
) -> ProviderRunStatePlan:
    return runtime_plan_provider_run_state(_runtime_request(request))


__all__ = [
    "ProviderRunStatePlan",
    "ProviderRunStatePlanRequest",
    "ProviderSessionDecision",
    "ProviderSessionPlanRequest",
    "ProviderSessionAdapter",
    "plan_provider_run_state",
    "plan_provider_session",
    "provider_session_adapter_for_service_name",
    "record_observed_provider_session_id",
    "record_successful_provider_session_metadata",
]
