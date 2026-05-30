from __future__ import annotations

import json
from pathlib import Path

_SERVICE_SESSION_ID_FILENAMES = {"codex": "thread_id", "opencode": "session_id"}
_SERVICE_SESSION_METADATA_FILENAME = "_service_session_metadata.json"


def service_session_id_path(role_session_path: Path, service_name: str) -> Path:
    filename = _SERVICE_SESSION_ID_FILENAMES.get(service_name, "thread_id")
    return role_session_path / service_name / filename


def service_session_metadata_path(role_session_path: Path) -> Path:
    return role_session_path / _SERVICE_SESSION_METADATA_FILENAME


def load_service_session_id(role_session_path: Path, service_name: str) -> str | None:
    path = service_session_id_path(role_session_path, service_name)
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


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
    path = state_dir / _SERVICE_SESSION_ID_FILENAMES.get(service_name, "thread_id")
    if not path.is_file():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def load_service_session_metadata(
    role_session_path: Path,
    service_name: str,
) -> dict[str, str] | None:
    path = service_session_metadata_path(role_session_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
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
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload[service_name] = {
        "service": service_name,
        "provider_session_id": session_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def is_service_session_metadata_path(path: Path) -> bool:
    return path.name == _SERVICE_SESSION_METADATA_FILENAME
