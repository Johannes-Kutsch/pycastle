from __future__ import annotations

import dataclasses
import json
import shutil
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import HardAgentError

if TYPE_CHECKING:
    from .resume import RunKind


_SERVICE_SESSION_ID_FILENAMES = {"codex": "thread_id", "opencode": "session_id"}
_SERVICE_SESSION_METADATA_FILENAME = "_service_session_metadata.json"


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


class RecoveredSessionIdPersistence(Enum):
    PERSIST = "persist"
    SKIP = "skip"


@dataclasses.dataclass(frozen=True)
class ProviderSessionDecision:
    run_kind: RunKind
    provider_session_id: str | None
    state_dir_relpath: str | None
    state_dir_path: Path | None
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    service_state_dir: Path | None = None
    exact_transcript_match: bool = False
    auth_seeding_requirement: AuthSeedingRequirement = (
        AuthSeedingRequirement.NOT_REQUIRED
    )
    auth_seed_action: LocalAuthSeedAction | None = None

    def container_state_dir(self, *, service_name: str) -> Path | None:
        if (
            service_name == "opencode"
            and getattr(self.run_kind, "value", None) == "resume"
            and self.service_state_dir is not None
        ):
            return self.service_state_dir
        return self.state_dir_path

    def container_state_dir_path(
        self,
        *,
        worktree: Path,
        service_name: str,
        container_workspace: str,
    ) -> str | None:
        container_state_dir = self.container_state_dir(service_name=service_name)
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


def load_provider_state_session_id(path: Path) -> str | None:
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


__all__ = [
    "AuthSeedingRequirement",
    "clear_service_session_metadata",
    "is_service_session_metadata_path",
    "load_exact_transcript_service_name",
    "load_service_session_id",
    "load_service_session_metadata",
    "load_service_session_metadata_payload",
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
