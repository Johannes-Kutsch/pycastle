from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from .service_resume_identity import is_exact_resumable_service_session
from .resume import (
    RoleSession,
    RunKind,
    ServiceSessionState,
    _normalize_state_dir_relpath,
    _role_provider_state_dir_relpath,
)

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


@dataclasses.dataclass(frozen=True)
class ProviderSessionPlan:
    state_dir_relpath: str | None
    host_state_dir: Path | None
    run_kind: RunKind
    provider_session_id: str | None
    requires_codex_auth_seed: bool = False


@dataclasses.dataclass(frozen=True)
class ProviderSessionPlanRequest:
    worktree: Path
    role: AgentRole
    namespace: str
    service: AgentService


@dataclasses.dataclass(frozen=True)
class PlannedProviderSession:
    plan: ProviderSessionPlan
    service_state_dir: Path | None
    exact_transcript_match: bool
    persist_provider_session_id: bool = False


def plan_provider_session(
    request: ProviderSessionPlanRequest,
) -> PlannedProviderSession:
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
    if (
        _preserves_role_provider_layout(request.service.name)
        and state_dir_relpath is not None
    ):
        state_dir_relpath = _role_provider_state_dir_relpath(
            request.role,
            request.namespace,
            request.service.name,
        )
        host_state_dir = request.worktree / state_dir_relpath.rstrip("/")

    if request.service.name == "claude":
        return _plan_claude_provider_session(
            role_session=role_session,
            service_state=service_state,
            state_dir_relpath=state_dir_relpath,
            host_state_dir=host_state_dir,
        )

    handoff = role_session.exact_transcript_handoff_for_service(request.service)
    provider_identity = handoff.provider_identity
    plan = ProviderSessionPlan(
        state_dir_relpath=state_dir_relpath,
        host_state_dir=host_state_dir,
        run_kind=provider_identity.run_kind,
        provider_session_id=provider_identity.provider_session_id,
        requires_codex_auth_seed=_requires_codex_auth_seed(
            request.service.name,
            host_state_dir,
        ),
    )
    return PlannedProviderSession(
        plan=plan,
        service_state_dir=service_state.state_dir,
        exact_transcript_match=handoff.is_eligible,
        persist_provider_session_id=provider_identity.persist_provider_session_id,
    )


def _plan_claude_provider_session(
    *,
    role_session: RoleSession,
    service_state: ServiceSessionState,
    state_dir_relpath: str | None,
    host_state_dir: Path | None,
) -> PlannedProviderSession:
    run_kind = (
        RunKind.RESUME if service_state.has_resumable_provider_state else RunKind.FRESH
    )
    provider_session_id = role_session.session_uuid()
    return PlannedProviderSession(
        plan=ProviderSessionPlan(
            state_dir_relpath=state_dir_relpath,
            host_state_dir=host_state_dir,
            run_kind=run_kind,
            provider_session_id=provider_session_id,
        ),
        service_state_dir=service_state.state_dir,
        exact_transcript_match=(
            run_kind is RunKind.RESUME
            and is_exact_resumable_service_session(
                role_session,
                "claude",
                provider_session_id=provider_session_id,
                provider_state_dir=host_state_dir,
            )
        ),
    )


def _preserves_role_provider_layout(service_name: str) -> bool:
    return service_name in {"codex", "opencode"}


def _requires_codex_auth_seed(
    service_name: str,
    host_state_dir: Path | None,
) -> bool:
    return (
        service_name == "codex"
        and host_state_dir is not None
        and not (host_state_dir / "auth.json").exists()
    )


__all__ = [
    "PlannedProviderSession",
    "ProviderSessionPlan",
    "ProviderSessionPlanRequest",
    "plan_provider_session",
]
