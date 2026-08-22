from __future__ import annotations

import json
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
    from pycastle.services import ServiceRegistry
    from pycastle.services.runtime_services import AgentService
    from pycastle.session.role import RoleSession

_SERVICE_SESSION_METADATA_FILENAME = "_service_session_metadata.json"
_SERVICE_SESSION_ID_FILENAMES = {"codex": "thread_id", "opencode": "session_id"}


def parse_service_session_metadata(
    payload: dict[str, object],
    service_name: str,
) -> dict[str, str] | None:
    metadata = payload.get(service_name)
    if not isinstance(metadata, dict):
        return None
    provider_session_id = metadata.get("provider_session_id")
    if not isinstance(provider_session_id, str) or not provider_session_id.strip():
        return None
    return {
        "service": service_name,
        "provider_session_id": provider_session_id.strip(),
    }


class ServiceSessionStore(ServiceResumeIdentityStore):
    """
    Owns all per-service session state anchored at a role-session directory.

    On-disk artifacts:
    - Metadata JSON: ``_service_session_metadata.json`` at the role-session root.
      Schema: ``{service_name: {"service": service_name, "provider_session_id": ...}}``,
      written with ``json.dumps(..., sort_keys=True)``.
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

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None:
        payload = self._load_metadata_payload()
        return (
            None
            if payload is None
            else parse_service_session_metadata(payload, service_name)
        )

    def exact_transcript_service_name(self) -> str | None:
        payload = self._load_metadata_payload()
        if payload is None or len(payload) != 1:
            return None
        service_name = next(iter(payload), None)
        if not isinstance(service_name, str) or not service_name:
            return None
        metadata = parse_service_session_metadata(payload, service_name)
        return None if metadata is None else metadata["service"]

    # --- Extended interface ---

    def get_service_session_id(self, service_name: str) -> str | None:
        return load_provider_state_session_id(self.session_id_path(service_name))

    def record_successful_run(
        self, service_name: str, provider_session_id: str | None = None
    ) -> None:
        legacy_fn = getattr(
            self._role_session, "record_successful_provider_session_metadata", None
        )
        if callable(legacy_fn):
            legacy_fn(service_name, provider_session_id)
        if provider_session_id is None:
            self._clear_metadata(service_name)
        else:
            self._save_metadata(service_name, provider_session_id)

    # --- Path helpers ---

    def session_id_path(self, service_name: str) -> Path:
        return ServiceSessionStore.provider_session_id_path(
            self.path / service_name, service_name
        )

    def metadata_path(self) -> Path:
        return self.path / _SERVICE_SESSION_METADATA_FILENAME

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

    # --- Internal helpers ---

    def _load_metadata_payload(self) -> dict[str, object] | None:
        path = self.metadata_path()
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _save_metadata(self, service_name: str, session_id: str) -> None:
        path = self.metadata_path()
        payload = self._load_metadata_payload() or {}
        payload[service_name] = {
            "service": service_name,
            "provider_session_id": session_id,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def _clear_metadata(self, service_name: str) -> None:
        path = self.metadata_path()
        payload = self._load_metadata_payload()
        if payload is None or service_name not in payload:
            return
        del payload[service_name]
        if not payload:
            path.unlink(missing_ok=True)
            return
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def store_for_role_session(role_session: RoleSession) -> ServiceSessionStore:
    return ServiceSessionStore(role_session.path, role_session)


def has_exact_transcript(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service: AgentService,
) -> bool:
    store = ServiceSessionStore(_role_session_path(worktree, role, namespace))
    if store.exact_transcript_service_name() != service.name:
        return False
    metadata = store.service_session_metadata(service.name)
    if metadata is None:
        return False
    provider_session_id = store.get_service_session_id(service.name)
    if (
        provider_session_id is None
        or metadata["provider_session_id"] != provider_session_id
    ):
        return False
    state_dir = _service_state_dir(worktree, role, namespace, service)
    if state_dir is None or not service.is_resumable(state_dir):
        return False
    return _is_exact_resumable_provider_session(
        service.name, provider_session_id, state_dir
    )


# --- Free-function façades ---


def provider_state_session_id_path(state_dir: Path, service_name: str) -> Path:
    return ServiceSessionStore.provider_session_id_path(state_dir, service_name)


def load_state_dir_provider_session_id(
    state_dir: Path | None,
    service_name: str,
) -> str | None:
    return ServiceSessionStore.load_state_session_id(state_dir, service_name)


def service_session_id_path(role_session_path: Path, service_name: str) -> Path:
    return ServiceSessionStore(role_session_path).session_id_path(service_name)


def service_session_metadata_path(role_session_path: Path) -> Path:
    return ServiceSessionStore(role_session_path).metadata_path()


def load_service_session_id(role_session_path: Path, service_name: str) -> str | None:
    return ServiceSessionStore(role_session_path).get_service_session_id(service_name)


def save_service_session_id(
    role_session_path: Path,
    service_name: str,
    session_id: str,
) -> None:
    ServiceSessionStore(role_session_path).save_service_session_id(
        service_name, session_id
    )


def load_service_session_metadata(
    role_session_path: Path,
    service_name: str,
) -> dict[str, str] | None:
    return ServiceSessionStore(role_session_path).service_session_metadata(service_name)


def save_service_session_metadata(
    role_session_path: Path,
    service_name: str,
    session_id: str,
) -> None:
    ServiceSessionStore(role_session_path).record_successful_run(
        service_name, session_id
    )


def clear_service_session_metadata(
    role_session_path: Path,
    service_name: str,
) -> None:
    ServiceSessionStore(role_session_path).record_successful_run(service_name)


def load_exact_transcript_service_name(role_session_path: Path) -> str | None:
    return ServiceSessionStore(role_session_path).exact_transcript_service_name()


def recover_state_dir_provider_session_id(
    state_dir: Path | None,
    service_name: str,
) -> str | None:
    return ServiceSessionStore.recover_state_session_id(state_dir, service_name)


def has_exact_provider_transcript_for_service(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service: AgentService,
) -> bool:
    return has_exact_transcript(
        worktree=worktree, role=role, namespace=namespace, service=service
    )


def has_exact_provider_transcript_for_selected_service(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    registry: ServiceRegistry | None,
    service_name: str,
) -> bool:
    if registry is None or not service_name:
        return False
    service = registry[service_name]
    if service is None:
        return False
    return has_exact_transcript(
        worktree=worktree, role=role, namespace=namespace, service=service
    )


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


def is_service_session_metadata_path(path: Path) -> bool:
    return path.name == _SERVICE_SESSION_METADATA_FILENAME


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
