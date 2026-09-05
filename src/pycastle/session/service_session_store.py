from __future__ import annotations

from typing import TYPE_CHECKING

from pycastle.runtime_session import (
    ServiceResumeIdentityStore,
    load_provider_state_session_id,
)
from pycastle.runtime_session import (
    is_exact_resumable_service_session as runtime_is_exact_resumable_service_session,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.agents.output_protocol import AgentRole
    from pycastle.services.runtime_services import AgentService
    from pycastle.session.role import RoleSession

_SERVICE_SESSION_ID_FILENAMES = {"codex": "thread_id", "opencode": "session_id"}


class ServiceSessionStore(ServiceResumeIdentityStore):
    """
    Owns all per-service session state anchored at a role-session directory.

    On-disk artifacts:
    - Per-service session-id file under ``<role_session_path>/<service_name>/``:
      ``thread_id`` for codex and all other services, ``session_id`` for opencode.
    """

    def __init__(self, path: Path, _role_session: object = None) -> None:
        self.path = path
        self._role_session = _role_session

    # --- ServiceResumeIdentityStore protocol ---

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        path = self.session_id_path(service_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")

    def transcript_owner_service_name(
        self, known_service_names: frozenset[str] | None = None
    ) -> str | None:
        if not self.path.is_dir():
            return None
        qualifying = [
            entry.name
            for entry in self.path.iterdir()
            if entry.is_dir()
            and (known_service_names is None or entry.name in known_service_names)
            and any(f.is_file() for f in entry.rglob("*"))
        ]
        return qualifying[0] if len(qualifying) == 1 else None

    # --- Extended interface ---

    def get_service_session_id(self, service_name: str) -> str | None:
        return load_provider_state_session_id(self.session_id_path(service_name))

    # --- Path helpers ---

    def session_id_path(self, service_name: str) -> Path:
        return ServiceSessionStore.provider_session_id_path(
            self.path / service_name, service_name
        )

    @staticmethod
    def provider_session_id_path(state_dir: Path, service_name: str) -> Path:
        return state_dir / _SERVICE_SESSION_ID_FILENAMES.get(service_name, "thread_id")

    @staticmethod
    def load_state_session_id(state_dir: Path | None, service_name: str) -> str | None:
        if state_dir is None:
            return None
        return load_provider_state_session_id(
            ServiceSessionStore.provider_session_id_path(state_dir, service_name)
        )

    @staticmethod
    def recover_state_session_id(
        state_dir: Path | None, service_name: str
    ) -> str | None:
        from pycastle.provider_session_adapter import (
            provider_session_adapter_for_service_name,
        )

        return provider_session_adapter_for_service_name(
            service_name
        ).recover_provider_session_id(state_dir)


def store_for_role_session(role_session: RoleSession) -> ServiceSessionStore:
    return ServiceSessionStore(role_session.path, role_session)


def has_exact_transcript(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service: AgentService,
    known_service_names: frozenset[str],
) -> bool:
    store = ServiceSessionStore(_role_session_path(worktree, role, namespace))
    if store.transcript_owner_service_name(known_service_names) != service.name:
        return False
    state_dir = _service_state_dir(worktree, role, namespace, service)
    if state_dir is None:
        return False
    return service.is_resumable(state_dir)


def is_exact_resumable_service_session(
    role_session: ServiceResumeIdentityStore,
    service_name: str,
    *,
    provider_session_id: str | None,
    provider_state_dir: Path | None,
) -> bool:
    return runtime_is_exact_resumable_service_session(
        role_session,
        service_name,
        provider_session_id=provider_session_id,
        provider_state_dir=provider_state_dir,
        exact_provider_session_matcher=lambda session_id, state_dir: (
            _is_exact_resumable_provider_session(service_name, session_id, state_dir)
        ),
    )


def _role_session_path(worktree: Path, role: AgentRole, namespace: str) -> Path:
    from pycastle.session.role import SESSION_DIR_NAME

    base = worktree / SESSION_DIR_NAME / role.value
    return base / namespace if namespace else base


def _service_state_dir(
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service: AgentService,
) -> Path | None:
    state_dir_relpath = service.state_dir_relpath(role, namespace)
    if state_dir_relpath is None:
        return None
    return worktree / state_dir_relpath.rstrip("/")


def _is_exact_resumable_provider_session(
    service_name: str,
    provider_session_id: str | None,
    provider_state_dir: Path | None,
) -> bool:
    from pycastle.provider_session_adapter import (
        provider_session_adapter_for_service_name,
    )

    return provider_session_adapter_for_service_name(
        service_name
    ).is_exact_resumable_provider_session(
        provider_session_id=provider_session_id,
        provider_state_dir=provider_state_dir,
    )
