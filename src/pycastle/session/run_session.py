from __future__ import annotations

import dataclasses
import shutil
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from ..errors import HardAgentError
from .resume import (
    RoleSession,
    RunKind,
    _normalize_state_dir_relpath,
    _role_provider_state_dir_relpath,
)

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


class RecoveredSessionIdPersistence(Enum):
    PERSIST = "persist"
    SKIP = "skip"


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


def _codex_auth_seeding_requirement(
    provider_state_dir: Path | None,
) -> AuthSeedingRequirement:
    if provider_state_dir is None:
        return AuthSeedingRequirement.NOT_REQUIRED
    if not (provider_state_dir / "auth.json").exists():
        return AuthSeedingRequirement.REQUIRED
    return AuthSeedingRequirement.NOT_REQUIRED


def _preserves_role_provider_layout(service_name: str) -> bool:
    return service_name in {"codex", "opencode"}


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
    auth_seed_action: LocalAuthSeedAction | None = None
    exact_transcript_match: bool = False

    def provider_state_dir_container_path(self, container_workspace: str) -> str | None:
        if (
            self.service.name == "opencode"
            and self.run_kind is RunKind.RESUME
            and self.service_state_dir is not None
        ):
            try:
                service_relpath = self.service_state_dir.relative_to(self.worktree)
            except ValueError:
                pass
            else:
                return f"{container_workspace}/{service_relpath.as_posix()}/"
        if self.host_provider_state_dir is not None:
            try:
                host_relpath = self.host_provider_state_dir.relative_to(self.worktree)
            except ValueError:
                pass
            else:
                return f"{container_workspace}/{host_relpath.as_posix()}/"
        if self.provider_state_dir_relpath is None:
            return None
        return f"{container_workspace}/{self.provider_state_dir_relpath}"

    def prepared_provider_session_id(self) -> str | None:
        provider_session_id = self.provider_session_id
        if provider_session_id is None:
            return None
        if (
            self.recovered_session_id_persistence
            is RecoveredSessionIdPersistence.PERSIST
        ):
            self.capture_provider_session_id(provider_session_id)
        return provider_session_id

    def prepare_host_provider_state_dir(self) -> None:
        if self.host_provider_state_dir is None:
            return
        self.host_provider_state_dir.mkdir(parents=True, exist_ok=True)
        if self.auth_seed_action is not None:
            self.auth_seed_action.apply()

    def capture_provider_session_id(self, provider_session_id: str) -> None:
        object.__setattr__(self, "provider_session_id", provider_session_id)
        if not _preserves_role_provider_layout(self.service.name):
            return
        RoleSession(
            self.worktree,
            self.role,
            self.namespace,
        ).save_service_session_id(self.service.name, provider_session_id)

    def record_successful_run(self, provider_session_id: str | None = None) -> None:
        session_id = provider_session_id or self.provider_session_id
        if session_id is None:
            return
        RoleSession(
            self.worktree,
            self.role,
            self.namespace,
        ).save_service_session_metadata(self.service.name, session_id)

    @classmethod
    def for_service(
        cls,
        *,
        role: AgentRole,
        worktree: Path,
        namespace: str,
        service: AgentService,
    ) -> RunSessionPlan:
        auth_seeding_requirement = AuthSeedingRequirement.NOT_REQUIRED
        recovered_session_id_persistence = RecoveredSessionIdPersistence.SKIP
        auth_seed_action: LocalAuthSeedAction | None = None
        role_session = RoleSession(worktree, role, namespace)
        service_state = role_session.service_session_state(service)
        service_state_dir = service_state.state_dir
        handoff = role_session.exact_transcript_handoff_for_service(service)
        provider_identity = handoff.provider_identity
        provider_session_id = provider_identity.provider_session_id
        run_kind = provider_identity.run_kind
        raw_provider_state_dir_relpath = service.state_dir_relpath(role, namespace)
        provider_state_dir_relpath = _normalize_state_dir_relpath(
            role,
            namespace,
            service.name,
            raw_provider_state_dir_relpath,
        )
        host_provider_state_dir = service_state_dir
        if (
            provider_state_dir_relpath is not None
            and provider_state_dir_relpath != raw_provider_state_dir_relpath
        ):
            host_provider_state_dir = worktree / provider_state_dir_relpath.rstrip("/")
        if (
            _preserves_role_provider_layout(service.name)
            and provider_state_dir_relpath is not None
        ):
            provider_state_dir_relpath = _role_provider_state_dir_relpath(
                role, namespace, service.name
            )
            host_provider_state_dir = worktree / provider_state_dir_relpath.rstrip("/")
        if provider_identity.persist_provider_session_id:
            recovered_session_id_persistence = RecoveredSessionIdPersistence.PERSIST
        if service.name == "codex":
            auth_seeding_requirement = _codex_auth_seeding_requirement(
                host_provider_state_dir
            )
            if (
                auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
                and host_provider_state_dir is not None
            ):
                auth_seed_action = LocalAuthSeedAction(
                    source=Path.home() / ".codex" / "auth.json",
                    destination=host_provider_state_dir / "auth.json",
                )
        exact_transcript_match = handoff.is_eligible
        return cls(
            role=role,
            worktree=worktree,
            namespace=namespace,
            service=service,
            run_kind=run_kind,
            service_state_dir=service_state_dir,
            provider_state_dir_relpath=provider_state_dir_relpath,
            host_provider_state_dir=host_provider_state_dir,
            provider_session_id=provider_session_id,
            auth_seeding_requirement=auth_seeding_requirement,
            recovered_session_id_persistence=recovered_session_id_persistence,
            auth_seed_action=auth_seed_action,
            exact_transcript_match=exact_transcript_match,
        )


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "RecoveredSessionIdPersistence",
    "RunSessionPlan",
]
