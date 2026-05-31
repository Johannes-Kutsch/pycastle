import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from ..services.provider_session_state import ProviderSessionStateRequest
from .provider_run_state import ProviderFreshFallbackReason, ProviderRunState
from .provider_session_state import (
    load_exact_transcript_service_name,
    is_service_session_metadata_path,
    load_service_session_id,
    load_service_session_metadata,
    save_service_session_id,
    save_service_session_metadata,
)
from .service_resume_identity import (
    is_exact_resumable_service_session,
    select_resumable_provider_session_id,
)

if TYPE_CHECKING:
    from ..services import ServiceRegistry
    from ..services.agent_service import AgentService

_NAMESPACE = uuid.NAMESPACE_DNS

SESSION_DIR_NAME = ".pycastle-session"


def _force_remove_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _role_provider_state_dir_relpath(
    role: AgentRole,
    namespace: str,
    service_name: str,
) -> str:
    if namespace:
        return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/{service_name}/"
    return f"{SESSION_DIR_NAME}/{role.value}/{service_name}/"


def _normalize_state_dir_relpath(
    role: AgentRole,
    namespace: str,
    service_name: str,
    state_dir_relpath: str | None,
) -> str | None:
    if state_dir_relpath is None or not namespace:
        return state_dir_relpath
    legacy_relpath = _role_provider_state_dir_relpath(role, "", service_name)
    if state_dir_relpath == legacy_relpath:
        return _role_provider_state_dir_relpath(role, namespace, service_name)
    return state_dir_relpath


class RunKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"


class ProviderIdentityKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"
    UNRECOVERABLE = "unrecoverable"


@dataclass(frozen=True)
class ProviderIdentity:
    kind: ProviderIdentityKind
    run_kind: RunKind
    provider_session_id: str | None
    persist_provider_session_id: bool = field(default=False, compare=False)

    def provider_run_state(
        self,
        *,
        provider_state_dir: Path | None,
    ) -> ProviderRunState:
        return ProviderRunState(
            run_kind=self.run_kind,
            provider_session_id=self.provider_session_id,
            persist_provider_session_id=self.persist_provider_session_id,
            provider_state_dir=provider_state_dir,
            fresh_fallback_reason=(
                ProviderFreshFallbackReason.UNRECOVERABLE_IDENTITY
                if self.kind is ProviderIdentityKind.UNRECOVERABLE
                else None
            ),
        )


@dataclass(frozen=True)
class ExactTranscriptHandoff:
    provider_identity: ProviderIdentity
    is_eligible: bool


@dataclass(frozen=True)
class ServiceSessionState:
    state_dir: Path | None
    has_resumable_provider_state: bool


def is_stage_done_for(worktree: Path, role: AgentRole) -> bool:
    return RoleSession(worktree, role).is_done()


def any_role_dir_present(worktree_path: Path) -> bool:
    session_base = worktree_path / SESSION_DIR_NAME
    if not session_base.is_dir():
        return False
    return any(d.is_dir() for d in session_base.iterdir())


class RoleSession:
    def __init__(self, worktree: Path, role: AgentRole, namespace: str = "") -> None:
        self._worktree = worktree
        self._role = role
        self._namespace = namespace

    @property
    def path(self) -> Path:
        base = self._worktree / SESSION_DIR_NAME / self._role.value
        return base / self._namespace if self._namespace else base

    def session_uuid(self) -> str:
        role_key = (
            f"pycastle.{self._role.value}.{self._namespace}"
            if self._namespace
            else f"pycastle.{self._role.value}"
        )
        role_ns = uuid.uuid5(_NAMESPACE, role_key)
        session_id = uuid.uuid5(role_ns, str(self._worktree.resolve()))
        return str(session_id)

    def service_session_id(self, service_name: str) -> str | None:
        return load_service_session_id(self.path, service_name)

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        save_service_session_id(self.path, service_name, session_id)

    def provider_identity(
        self,
        service_name: str,
        *,
        has_resumable_provider_state: bool,
        provider_state_dir: Path | None = None,
        derived_provider_session_id: str | None = None,
        persist_provider_session_id: bool = False,
    ) -> ProviderIdentity:
        if derived_provider_session_id is not None:
            if has_resumable_provider_state:
                return ProviderIdentity(
                    ProviderIdentityKind.RESUME,
                    RunKind.RESUME,
                    derived_provider_session_id,
                    persist_provider_session_id=persist_provider_session_id,
                )
            return ProviderIdentity(
                ProviderIdentityKind.FRESH,
                RunKind.FRESH,
                derived_provider_session_id,
                persist_provider_session_id=persist_provider_session_id,
            )

        if not has_resumable_provider_state:
            return ProviderIdentity(ProviderIdentityKind.FRESH, RunKind.FRESH, None)

        selection = select_resumable_provider_session_id(
            self,
            service_name,
            provider_state_dir=provider_state_dir or (self.path / service_name),
            has_resumable_provider_state=has_resumable_provider_state,
        )
        provider_session_id = selection.provider_session_id
        if provider_session_id is None:
            return ProviderIdentity(
                ProviderIdentityKind.UNRECOVERABLE, RunKind.FRESH, None
            )

        return ProviderIdentity(
            ProviderIdentityKind.RESUME,
            RunKind.RESUME,
            provider_session_id,
            persist_provider_session_id=selection.persist_provider_session_id,
        )

    def is_exact_resumable_provider_session(
        self,
        service_name: str,
        provider_session_id: str | None,
        provider_state_dir: Path | None,
    ) -> bool:
        return is_exact_resumable_service_session(
            self,
            service_name,
            provider_session_id=provider_session_id,
            provider_state_dir=provider_state_dir,
        )

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None:
        return load_service_session_metadata(self.path, service_name)

    def exact_transcript_service_name(self) -> str | None:
        return load_exact_transcript_service_name(self.path)

    def save_service_session_metadata(self, service_name: str, session_id: str) -> None:
        save_service_session_metadata(self.path, service_name, session_id)

    def service_session_state(self, service: "AgentService") -> ServiceSessionState:
        state_dir_relpath = _normalize_state_dir_relpath(
            self._role,
            self._namespace,
            service.name,
            service.state_dir_relpath(self._role, self._namespace),
        )
        state_dir = (
            self._worktree / state_dir_relpath
            if state_dir_relpath is not None
            else None
        )
        return ServiceSessionState(
            state_dir=state_dir,
            has_resumable_provider_state=(
                state_dir is not None and service.is_resumable(state_dir)
            ),
        )

    def exact_transcript_handoff(
        self,
        service_name: str,
        *,
        state_dir: Path | None,
        has_resumable_provider_state: bool,
        derived_provider_session_id: str | None = None,
        persist_provider_session_id: bool = False,
    ) -> ExactTranscriptHandoff:
        provider_identity = self.provider_identity(
            service_name,
            has_resumable_provider_state=has_resumable_provider_state,
            provider_state_dir=state_dir,
            derived_provider_session_id=derived_provider_session_id,
            persist_provider_session_id=persist_provider_session_id,
        )

        is_eligible = (
            provider_identity.run_kind is RunKind.RESUME
            and not provider_identity.persist_provider_session_id
            and is_exact_resumable_service_session(
                self,
                service_name,
                provider_session_id=provider_identity.provider_session_id,
                provider_state_dir=state_dir,
            )
        )
        return ExactTranscriptHandoff(
            provider_identity=provider_identity,
            is_eligible=is_eligible,
        )

    def exact_transcript_handoff_for_service(
        self, service: "AgentService"
    ) -> ExactTranscriptHandoff:
        state = self.service_session_state(service)
        provider_session_state = service.provider_session_state(
            ProviderSessionStateRequest(
                role_session=self,
                provider_state_dir=state.state_dir,
                has_resumable_provider_state=state.has_resumable_provider_state,
                require_exact_transcript_match=True,
            )
        )
        provider_identity = self.provider_identity(
            service.name,
            has_resumable_provider_state=state.has_resumable_provider_state,
            provider_state_dir=state.state_dir,
            derived_provider_session_id=provider_session_state.provider_session_id,
            persist_provider_session_id=provider_session_state.persist_provider_session_id,
        )
        return ExactTranscriptHandoff(
            provider_identity=provider_identity,
            is_eligible=provider_session_state.exact_transcript_match,
        )

    def has_exact_transcript_handoff_for_selected_service(
        self,
        registry: "ServiceRegistry | None",
        service_name: str,
    ) -> bool:
        if registry is None or not service_name:
            return False
        service = registry[service_name]
        if service is None:
            return False
        return self.exact_transcript_handoff_for_service(service).is_eligible

    def is_resumable(self) -> bool:
        return self.path.is_dir() and any(
            f.is_file() and not is_service_session_metadata_path(f)
            for f in self.path.rglob("*")
        )

    def is_done(self) -> bool:
        return self.path.is_dir() and not self.is_resumable()

    def run_kind(self) -> RunKind:
        return RunKind.RESUME if self.is_resumable() else RunKind.FRESH

    def start_fresh(self) -> None:
        if self.path.is_dir():
            shutil.rmtree(self.path, onerror=_force_remove_readonly)
        self.path.mkdir(parents=True, exist_ok=True)

    def mark_done(self) -> None:
        if not self.path.is_dir():
            return
        for child in self.path.iterdir():
            if is_service_session_metadata_path(child):
                continue
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, onerror=_force_remove_readonly)

    def discard(self) -> None:
        if self.path.is_dir():
            shutil.rmtree(self.path, onerror=_force_remove_readonly)
