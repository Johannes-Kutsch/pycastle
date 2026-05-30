from __future__ import annotations

import dataclasses
import shutil
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from ..errors import HardAgentError
from ._provider_session_sidecars import (
    save_service_session_id,
    save_service_session_metadata,
)
from .resume import (
    RoleSession,
    RunKind,
    ServiceSessionState,
    _normalize_state_dir_relpath,
    _role_provider_state_dir_relpath,
)

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


@dataclasses.dataclass(frozen=True)
class LocalAuthSeedAction:
    source: Path
    destination: Path
    missing_source_message: str = dataclasses.field(
        default="Codex authentication missing: run `codex login` on the host.",
        compare=False,
    )

    def require_source(self) -> Path:
        if not self.source.exists():
            raise HardAgentError(
                self.missing_source_message,
                status_code=401,
            )
        return self.source

    def apply(self) -> None:
        if self.destination.exists():
            return
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source, self.destination)


@dataclasses.dataclass(frozen=True)
class ProviderSessionPlan:
    state_dir_relpath: str | None
    host_state_dir: Path | None
    run_kind: RunKind
    provider_session_id: str | None
    auth_seeding_requirement: AuthSeedingRequirement = (
        AuthSeedingRequirement.NOT_REQUIRED
    )
    auth_seed_action: LocalAuthSeedAction | None = None

    def container_state_dir(
        self,
        *,
        service_name: str,
        service_state_dir: Path | None,
    ) -> Path | None:
        if (
            service_name == "opencode"
            and self.run_kind is RunKind.RESUME
            and service_state_dir is not None
        ):
            return service_state_dir
        return self.host_state_dir

    def container_state_dir_path(
        self,
        *,
        worktree: Path,
        service_name: str,
        service_state_dir: Path | None,
        container_workspace: str,
    ) -> str | None:
        container_state_dir = self.container_state_dir(
            service_name=service_name,
            service_state_dir=service_state_dir,
        )
        if container_state_dir is not None:
            try:
                container_relpath = container_state_dir.relative_to(worktree)
            except ValueError:
                pass
            else:
                return f"{container_workspace}/{container_relpath.as_posix()}/"
        if self.state_dir_relpath is None:
            return None
        return f"{container_workspace}/{self.state_dir_relpath}"


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
    if not _preserves_role_provider_layout(service_name):
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
    provider_session_id: str,
) -> None:
    save_service_session_metadata(
        RoleSession(worktree, role, namespace).path,
        service_name,
        provider_session_id,
    )


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
            service=request.service,
            service_state=service_state,
            state_dir_relpath=state_dir_relpath,
            host_state_dir=host_state_dir,
        )

    provider_run_state = request.service.resolve_provider_run_state(
        role_session,
        provider_state_dir=service_state.state_dir,
        has_resumable_provider_state=service_state.has_resumable_provider_state,
    )
    plan = ProviderSessionPlan(
        state_dir_relpath=state_dir_relpath,
        host_state_dir=host_state_dir,
        run_kind=provider_run_state.run_kind,
        provider_session_id=provider_run_state.provider_session_id,
        auth_seeding_requirement=_codex_auth_seeding_requirement(
            request.service.name,
            host_state_dir,
        ),
        auth_seed_action=_codex_auth_seed_action(
            request.service.name,
            host_state_dir,
        ),
    )
    return PlannedProviderSession(
        plan=plan,
        service_state_dir=service_state.state_dir,
        exact_transcript_match=(
            provider_run_state.run_kind is RunKind.RESUME
            and not provider_run_state.persist_provider_session_id
            and request.service.has_exact_transcript_session(
                role_session,
                provider_run_state=provider_run_state,
                provider_state_dir=service_state.state_dir,
            )
        ),
        persist_provider_session_id=provider_run_state.persist_provider_session_id,
    )


def _plan_claude_provider_session(
    *,
    role_session: RoleSession,
    service: AgentService,
    service_state: ServiceSessionState,
    state_dir_relpath: str | None,
    host_state_dir: Path | None,
) -> PlannedProviderSession:
    provider_run_state = service.resolve_provider_run_state(
        role_session,
        provider_state_dir=host_state_dir,
        has_resumable_provider_state=service_state.has_resumable_provider_state,
    )
    return PlannedProviderSession(
        plan=ProviderSessionPlan(
            state_dir_relpath=state_dir_relpath,
            host_state_dir=host_state_dir,
            run_kind=provider_run_state.run_kind,
            provider_session_id=provider_run_state.provider_session_id,
        ),
        service_state_dir=service_state.state_dir,
        exact_transcript_match=(
            provider_run_state.run_kind is RunKind.RESUME
            and service.has_exact_transcript_session(
                role_session,
                provider_run_state=provider_run_state,
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


def _codex_auth_seeding_requirement(
    service_name: str,
    host_state_dir: Path | None,
) -> AuthSeedingRequirement:
    if _requires_codex_auth_seed(service_name, host_state_dir):
        return AuthSeedingRequirement.REQUIRED
    return AuthSeedingRequirement.NOT_REQUIRED


def _codex_auth_seed_action(
    service_name: str,
    host_state_dir: Path | None,
) -> LocalAuthSeedAction | None:
    if not _requires_codex_auth_seed(service_name, host_state_dir):
        return None
    if host_state_dir is None:
        return None
    return LocalAuthSeedAction(
        source=Path.home() / ".codex" / "auth.json",
        destination=host_state_dir / "auth.json",
    )


__all__ = [
    "AuthSeedingRequirement",
    "capture_provider_session_id",
    "LocalAuthSeedAction",
    "PlannedProviderSession",
    "ProviderSessionPlan",
    "ProviderSessionPlanRequest",
    "plan_provider_session",
]
