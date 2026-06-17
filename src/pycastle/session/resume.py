import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pycastle_agent_runtime.contracts import AgentService as RuntimeAgentService
from pycastle_agent_runtime.roles import AgentRole
from pycastle_agent_runtime.session import (
    RunKind,
    normalize_state_dir_relpath,
    provider_state_relpath as runtime_provider_state_relpath,
)

from ._provider_session_decision import RecoveredSessionIdPersistence
from .provider_run_state import ProviderFreshFallbackReason, ProviderRunState
from .provider_session_state import (
    clear_service_session_metadata,
    has_exact_provider_transcript_for_service,
    is_exact_resumable_service_session,
    load_service_session_id as load_role_service_session_id,
    load_exact_transcript_service_name,
    is_service_session_metadata_path,
    load_service_session_metadata,
    save_service_session_metadata,
    service_session_id_path as role_service_session_id_path,
)

if TYPE_CHECKING:
    from ..services import ServiceRegistry
    from ..services.agent_service import AgentService

_NAMESPACE = uuid.NAMESPACE_DNS
SESSION_DIR_NAME = ".pycastle-session"
_SESSION_UUID_SEED_FILENAME = "_session_uuid_seed"


def _force_remove_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def provider_state_relpath(
    role: AgentRole,
    provider_name: str,
    namespace: str = "",
) -> str:
    return runtime_provider_state_relpath(
        role,
        provider_name,
        namespace,
        session_root=SESSION_DIR_NAME,
    )


def _normalize_state_dir_relpath(
    role: AgentRole,
    namespace: str,
    service_name: str,
    state_dir_relpath: str | None,
) -> str | None:
    return normalize_state_dir_relpath(
        role,
        namespace,
        service_name,
        state_dir_relpath,
    )


@dataclass(frozen=True)
class ServiceSessionState:
    state_dir: Path | None
    has_resumable_provider_state: bool
    state_dir_relpath: str | None = None


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

    @staticmethod
    def provider_state_relpath_for(
        role: AgentRole,
        provider_name: str,
        namespace: str = "",
    ) -> str:
        return runtime_provider_state_relpath(
            role,
            provider_name,
            namespace,
            session_root=SESSION_DIR_NAME,
        )

    def provider_state_relpath(self, provider_name: str) -> str:
        return self.provider_state_relpath_for(
            self._role,
            provider_name,
            self._namespace,
        ).rstrip("/")

    def session_uuid(self) -> str:
        role_key = (
            f"pycastle.{self._role.value}.{self._namespace}"
            if self._namespace
            else f"pycastle.{self._role.value}"
        )
        role_ns = uuid.uuid5(_NAMESPACE, role_key)
        session_id = uuid.uuid5(
            role_ns,
            f"{self._worktree.resolve()}:{self._ensure_session_uuid_seed()}",
        )
        return str(session_id)

    def _session_uuid_seed_path(self) -> Path:
        return self.path / _SESSION_UUID_SEED_FILENAME

    def _ensure_session_uuid_seed(self) -> str:
        path = self._session_uuid_seed_path()
        if path.is_file():
            seed = path.read_text(encoding="utf-8").strip()
            if seed:
                return seed
        seed = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seed, encoding="utf-8")
        return seed

    def provider_state_dir(self, provider_name: str) -> Path:
        return self._worktree / self.provider_state_relpath(provider_name)

    def service_session_id_path(self, service_name: str) -> Path:
        return role_service_session_id_path(self.path, service_name)

    def service_session_id(self, service_name: str) -> str | None:
        return load_role_service_session_id(self.path, service_name)

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        path = self.service_session_id_path(service_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")

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

    def record_successful_provider_session_metadata(
        self,
        service_name: str,
        provider_session_id: str | None,
    ) -> None:
        if provider_session_id is None:
            clear_service_session_metadata(self.path, service_name)
            return
        save_service_session_metadata(self.path, service_name, provider_session_id)

    def has_exact_provider_transcript_for_service(
        self,
        service: RuntimeAgentService,
    ) -> bool:
        return has_exact_provider_transcript_for_service(
            worktree=self._worktree,
            role=self._role,
            namespace=self._namespace,
            service=service,
        )

    def has_exact_provider_transcript_for_selected_service(
        self,
        registry: "ServiceRegistry | None",
        service_name: str,
    ) -> bool:
        if registry is None or not service_name:
            return False
        service = registry[service_name]
        if service is None:
            return False
        return self.has_exact_provider_transcript_for_service(service)

    def service_session_state(
        self, service: RuntimeAgentService
    ) -> ServiceSessionState:
        state_dir_relpath = _normalize_state_dir_relpath(
            self._role,
            self._namespace,
            service.name,
            service.state_dir_relpath(self._role, self._namespace),
        )
        state_dir = (
            self.provider_state_dir(service.name)
            if state_dir_relpath
            == provider_state_relpath(
                self._role,
                service.name,
                self._namespace,
            )
            else self._worktree / state_dir_relpath
            if state_dir_relpath is not None
            else None
        )
        return ServiceSessionState(
            state_dir=state_dir,
            has_resumable_provider_state=(
                state_dir is not None and service.is_resumable(state_dir)
            ),
            state_dir_relpath=state_dir_relpath,
        )

    def provider_run_state_for_service(
        self,
        service: "AgentService",
    ) -> ProviderRunState:
        from ._provider_session_plan import (
            ProviderRunStatePlanRequest,
            plan_provider_run_state,
        )

        plan = plan_provider_run_state(
            ProviderRunStatePlanRequest(
                worktree=self._worktree,
                role=self._role,
                namespace=self._namespace,
                service=service,
            )
        )
        return ProviderRunState(
            run_kind=plan.run_kind,
            provider_session_id=plan.provider_session_id,
            persist_provider_session_id=(
                plan.recovered_session_id_persistence
                is RecoveredSessionIdPersistence.PERSIST
            ),
            provider_state_dir=plan.provider_state_dir,
            fresh_fallback_reason=(
                ProviderFreshFallbackReason.UNRECOVERABLE_IDENTITY
                if (
                    plan.run_kind is RunKind.FRESH
                    and plan.provider_state_dir is not None
                    and service.is_resumable(plan.provider_state_dir)
                    and plan.provider_session_id is None
                )
                else None
            ),
        )

    def has_exact_transcript_handoff_for_selected_service(
        self,
        registry: "ServiceRegistry | None",
        service_name: str,
    ) -> bool:
        return self.has_exact_provider_transcript_for_selected_service(
            registry,
            service_name,
        )

    def is_resumable(self) -> bool:
        return self.path.is_dir() and any(
            f.is_file()
            and not is_service_session_metadata_path(f)
            and f.name != _SESSION_UUID_SEED_FILENAME
            for f in self.path.rglob("*")
        )

    def is_done(self) -> bool:
        return self.path.is_dir() and not self.is_resumable()

    def run_kind(self) -> RunKind:
        return RunKind.RESUME if self.is_resumable() else RunKind.FRESH

    def start_fresh(self) -> None:
        seed = self._ensure_session_uuid_seed()
        if self.path.is_dir():
            shutil.rmtree(self.path, onerror=_force_remove_readonly)
        self.path.mkdir(parents=True, exist_ok=True)
        self._session_uuid_seed_path().write_text(seed, encoding="utf-8")

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
