from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from ._provider_session_decision import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
)
from .provider_session_state import (
    clear_service_session_metadata,
    save_service_session_id,
    save_service_session_metadata,
)
from .resume import (
    RoleSession,
    _normalize_state_dir_relpath,
)
from ..services.provider_session_state import (
    ProviderSessionStateRequest as ServiceProviderSessionStateRequest,
)

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class ProviderSessionPlanRequest:
    worktree: Path
    role: AgentRole
    namespace: str
    service: AgentService


def record_observed_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    if service_name == "opencode" and service_state_dir is not None:
        session_id_path = service_state_dir / "session_id"
        session_id_path.parent.mkdir(parents=True, exist_ok=True)
        session_id_path.write_text(provider_session_id, encoding="utf-8")
    if service_name not in {"codex", "opencode"}:
        return
    save_service_session_id(
        RoleSession(worktree, role, namespace).path,
        service_name,
        provider_session_id,
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
    role_session_path = RoleSession(worktree, role, namespace).path
    if provider_session_id is None:
        clear_service_session_metadata(role_session_path, service_name)
        return
    save_service_session_metadata(
        role_session_path,
        service_name,
        provider_session_id,
    )


def plan_provider_session(
    request: ProviderSessionPlanRequest,
) -> ProviderSessionDecision:
    role_session = RoleSession(request.worktree, request.role, request.namespace)
    service_state = role_session.service_session_state(request.service)

    raw_state_dir_relpath = request.service.state_dir_relpath(
        request.role,
        request.namespace,
    )
    state_dir_relpath = _normalize_state_dir_relpath(
        request.role,
        request.namespace,
        request.service.name,
        raw_state_dir_relpath,
    )
    host_state_dir = service_state.state_dir
    if state_dir_relpath is not None and state_dir_relpath != raw_state_dir_relpath:
        host_state_dir = request.worktree / state_dir_relpath.rstrip("/")

    provider_session_state = request.service.provider_session_state(
        ServiceProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=host_state_dir,
            has_resumable_provider_state=service_state.has_resumable_provider_state,
            state_dir_relpath=state_dir_relpath,
            require_exact_transcript_match=True,
            preferred_provider_session_id=(
                role_session.session_uuid()
                if request.service.name == "claude"
                else None
            ),
        )
    )
    exact_transcript_handoff = role_session.exact_transcript_handoff_for_service(
        request.service
    )
    provider_identity = exact_transcript_handoff.provider_identity
    recovered_session_id_persistence = RecoveredSessionIdPersistence.SKIP
    if provider_identity.persist_provider_session_id:
        recovered_session_id_persistence = RecoveredSessionIdPersistence.PERSIST
    return ProviderSessionDecision(
        run_kind=provider_identity.run_kind,
        provider_session_id=provider_identity.provider_session_id,
        state_dir_relpath=provider_session_state.state_dir_relpath or state_dir_relpath,
        state_dir_path=provider_session_state.state_dir_path or host_state_dir,
        service_state_dir=service_state.state_dir,
        recovered_session_id_persistence=recovered_session_id_persistence,
        exact_transcript_match=exact_transcript_handoff.is_eligible,
        auth_seeding_requirement=(
            provider_session_state.auth_seeding_requirement
            or AuthSeedingRequirement.NOT_REQUIRED
        ),
        auth_seed_action=provider_session_state.auth_seed_action,
    )


__all__ = [
    "AuthSeedingRequirement",
    "capture_provider_session_id",
    "LocalAuthSeedAction",
    "ProviderSessionDecision",
    "ProviderSessionPlanRequest",
    "plan_provider_session",
]
