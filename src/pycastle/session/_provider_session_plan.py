from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from pycastle_agent_runtime.session_planning import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
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
from .resume import RoleSession

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class ProviderRunStatePlanRequest:
    worktree: Path
    role: AgentRole
    namespace: str
    service: AgentService


ProviderSessionPlanRequest = ProviderRunStatePlanRequest


def record_observed_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    runtime_record_observed_provider_session_id(
        provider_run_state_plan=ProviderRunStatePlan(
            role_session=RoleSession(worktree, role, namespace),
            service_name=service_name,
            run_kind=RoleSession(worktree, role, namespace).run_kind(),
            provider_state_dir=service_state_dir,
            provider_state_dir_relpath=None,
            provider_session_id=None,
            requires_host_codex_auth=False,
            recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
            service_state_dir=service_state_dir,
        ),
        provider_session_id=provider_session_id,
    )


def capture_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    record_observed_provider_session_id(
        worktree=worktree,
        role=role,
        namespace=namespace,
        service_name=service_name,
        service_state_dir=service_state_dir,
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
            role_session=RoleSession(worktree, role, namespace),
            service_name=service_name,
            run_kind=RoleSession(worktree, role, namespace).run_kind(),
            provider_state_dir=None,
            provider_state_dir_relpath=None,
            provider_session_id=provider_session_id,
            requires_host_codex_auth=False,
            recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
        ),
        provider_session_id=provider_session_id,
    )


def plan_provider_session(
    request: ProviderRunStatePlanRequest,
) -> ProviderSessionDecision:
    return runtime_plan_provider_session(
        RuntimeProviderRunStatePlanRequest(
            worktree=request.worktree,
            role=request.role,
            namespace=request.namespace,
            service=request.service,
            role_session=RoleSession(request.worktree, request.role, request.namespace),
        )
    )


def plan_provider_run_state(
    request: ProviderRunStatePlanRequest,
) -> ProviderRunStatePlan:
    return runtime_plan_provider_run_state(
        RuntimeProviderRunStatePlanRequest(
            worktree=request.worktree,
            role=request.role,
            namespace=request.namespace,
            service=request.service,
            role_session=RoleSession(request.worktree, request.role, request.namespace),
        )
    )


__all__ = [
    "AuthSeedingRequirement",
    "capture_provider_session_id",
    "LocalAuthSeedAction",
    "ProviderRunStatePlan",
    "ProviderRunStatePlanRequest",
    "ProviderSessionDecision",
    "ProviderSessionPlanRequest",
    "plan_provider_run_state",
    "plan_provider_session",
    "record_observed_provider_session_id",
    "record_successful_provider_session_metadata",
]
