from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from ._provider_session_decision import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
)

if TYPE_CHECKING:
    from ..services import ServiceRegistry
    from ..services.agent_service import AgentService


_SERVICE_SESSION_ID_FILENAMES = {"codex": "thread_id", "opencode": "session_id"}
_SERVICE_SESSION_METADATA_FILENAME = "_service_session_metadata.json"


def service_session_id_path(role_session_path: Path, service_name: str) -> Path:
    filename = _SERVICE_SESSION_ID_FILENAMES.get(service_name, "thread_id")
    return role_session_path / service_name / filename


def service_session_metadata_path(role_session_path: Path) -> Path:
    return role_session_path / _SERVICE_SESSION_METADATA_FILENAME


def load_service_session_id(role_session_path: Path, service_name: str) -> str | None:
    return load_provider_state_session_id(
        service_session_id_path(role_session_path, service_name)
    )


def save_service_session_id(
    role_session_path: Path,
    service_name: str,
    session_id: str,
) -> None:
    path = service_session_id_path(role_session_path, service_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(session_id, encoding="utf-8")


def load_state_dir_provider_session_id(
    state_dir: Path | None,
    service_name: str,
) -> str | None:
    if state_dir is None:
        return None
    filename = _SERVICE_SESSION_ID_FILENAMES.get(service_name, "thread_id")
    return load_provider_state_session_id(state_dir / filename)


def recover_state_dir_provider_session_id(
    state_dir: Path | None,
    service_name: str,
) -> str | None:
    if service_name != "codex":
        return None
    return _recover_codex_rollout_thread_id(state_dir)


def load_provider_state_session_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def _recover_codex_rollout_thread_id(state_dir: Path | None) -> str | None:
    if state_dir is None:
        return None
    sessions_dir = state_dir / "sessions"
    if not sessions_dir.is_dir():
        return None

    found: set[str] = set()
    for rollout in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            lines = rollout.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "thread.started":
                continue
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                found.add(thread_id.strip())

    return next(iter(found)) if len(found) == 1 else None


def load_service_session_metadata(
    role_session_path: Path,
    service_name: str,
) -> dict[str, str] | None:
    payload = load_service_session_metadata_payload(role_session_path)
    if payload is None:
        return None
    return parse_service_session_metadata(payload, service_name)


def load_exact_transcript_service_name(role_session_path: Path) -> str | None:
    payload = load_service_session_metadata_payload(role_session_path)
    if payload is None or len(payload) != 1:
        return None
    service_name = next(iter(payload), None)
    if not isinstance(service_name, str) or not service_name:
        return None
    metadata = parse_service_session_metadata(payload, service_name)
    if metadata is None:
        return None
    return metadata["service"]


def load_service_session_metadata_payload(
    role_session_path: Path,
) -> dict[str, object] | None:
    path = service_session_metadata_path(role_session_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


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


def save_service_session_metadata(
    role_session_path: Path,
    service_name: str,
    session_id: str,
) -> None:
    path = service_session_metadata_path(role_session_path)
    payload = load_service_session_metadata_payload(role_session_path) or {}
    payload[service_name] = {
        "service": service_name,
        "provider_session_id": session_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def clear_service_session_metadata(
    role_session_path: Path,
    service_name: str,
) -> None:
    path = service_session_metadata_path(role_session_path)
    payload = load_service_session_metadata_payload(role_session_path)
    if payload is None or service_name not in payload:
        return
    del payload[service_name]
    if not payload:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def is_service_session_metadata_path(path: Path) -> bool:
    return path.name == _SERVICE_SESSION_METADATA_FILENAME


def has_exact_provider_transcript_for_service(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service: "AgentService",
) -> bool:
    role_session_path = _role_session_path(worktree, role, namespace)
    if load_exact_transcript_service_name(role_session_path) != service.name:
        return False

    metadata = load_service_session_metadata(role_session_path, service.name)
    if metadata is None:
        return False

    provider_session_id = load_service_session_id(role_session_path, service.name)
    if (
        provider_session_id is None
        or metadata["provider_session_id"] != provider_session_id
    ):
        return False

    state_dir = _service_state_dir(worktree, role, namespace, service)
    if state_dir is None or not service.is_resumable(state_dir):
        return False

    if service.name != "codex":
        return True

    exact_provider_session_id = _exact_provider_session_id_from_state_dir(
        state_dir, service.name
    )
    return exact_provider_session_id == provider_session_id


def has_exact_provider_transcript_for_selected_service(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    registry: "ServiceRegistry | None",
    service_name: str,
) -> bool:
    if registry is None or not service_name:
        return False
    service = registry[service_name]
    if service is None:
        return False
    return has_exact_provider_transcript_for_service(
        worktree=worktree,
        role=role,
        namespace=namespace,
        service=service,
    )


def _role_session_path(worktree: Path, role: AgentRole, namespace: str) -> Path:
    base = worktree / ".pycastle-session" / role.value
    return base / namespace if namespace else base


def _service_state_dir(
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service: "AgentService",
) -> Path | None:
    state_dir_relpath = service.state_dir_relpath(role, namespace)
    if state_dir_relpath is None:
        return None
    return worktree / state_dir_relpath.rstrip("/")


def _exact_provider_session_id_from_state_dir(
    state_dir: Path,
    service_name: str,
) -> str | None:
    if service_name == "codex":
        return recover_state_dir_provider_session_id(state_dir, service_name)
    return None


__all__ = [
    "AuthSeedingRequirement",
    "clear_service_session_metadata",
    "has_exact_provider_transcript_for_selected_service",
    "has_exact_provider_transcript_for_service",
    "is_service_session_metadata_path",
    "load_exact_transcript_service_name",
    "load_service_session_id",
    "load_service_session_metadata",
    "load_service_session_metadata_payload",
    "recover_state_dir_provider_session_id",
    "load_state_dir_provider_session_id",
    "LocalAuthSeedAction",
    "parse_service_session_metadata",
    "ProviderSessionDecision",
    "RecoveredSessionIdPersistence",
    "save_service_session_id",
    "save_service_session_metadata",
    "service_session_id_path",
    "service_session_metadata_path",
]
