import os
import shutil
import stat
import uuid
from enum import Enum
from pathlib import Path

from ..agents.output_protocol import AgentRole

_NAMESPACE = uuid.NAMESPACE_DNS

SESSION_DIR_NAME = ".pycastle-session"

_SERVICE_SESSION_ID_FILENAMES = {
    "codex": "thread_id",
    "opencode": "session_id",
}
_SERVICE_SESSION_METADATA_FILENAME = "_service_session_metadata.json"


def _force_remove_readonly(func, path, _exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class RunKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"


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

    def service_session_id_path(self, service_name: str) -> Path:
        filename = _SERVICE_SESSION_ID_FILENAMES.get(service_name, "thread_id")
        return self.path / service_name / filename

    @property
    def service_session_metadata_path(self) -> Path:
        return self.path / _SERVICE_SESSION_METADATA_FILENAME

    def service_session_id(self, service_name: str) -> str | None:
        path = self.service_session_id_path(service_name)
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        path = self.service_session_id_path(service_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id, encoding="utf-8")

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None:
        path = self.service_session_metadata_path
        if not path.is_file():
            return None
        try:
            import json

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

    def save_service_session_metadata(self, service_name: str, session_id: str) -> None:
        path = self.service_session_metadata_path
        try:
            import json

            payload = (
                json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            )
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

    def is_resumable(self) -> bool:
        return self.path.is_dir() and any(
            f.is_file() and f.name != _SERVICE_SESSION_METADATA_FILENAME
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
            if child.name == _SERVICE_SESSION_METADATA_FILENAME:
                continue
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, onerror=_force_remove_readonly)

    def discard(self) -> None:
        if self.path.is_dir():
            shutil.rmtree(self.path, onerror=_force_remove_readonly)
