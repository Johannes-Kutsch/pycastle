import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import (
    RunKind,
)
from pycastle.runtime_session import (
    provider_state_relpath as runtime_provider_state_relpath,
)
from pycastle.runtime_session import (
    session_uuid as runtime_session_uuid,
)
from pycastle.session.service_session_store import is_service_session_metadata_path

SESSION_DIR_NAME = ".pycastle-session"
_CONTINUATION_FILENAME = "_continuation"
_DONE_FILENAME = "_done"
_FINGERPRINT_FILENAME = "_fingerprint"


def session_uuid_for_role_session_path(role_session_path: Path) -> str | None:
    identity = _role_session_identity_from_path(role_session_path)
    if identity is None:
        return None
    worktree, role, namespace = identity
    return runtime_session_uuid(worktree, role.value, namespace)


def _role_session_identity_from_path(
    role_session_path: Path,
) -> tuple[Path, AgentRole, str] | None:
    path = role_session_path.resolve()
    parts = path.parts
    try:
        session_root_index = (
            len(parts) - 1 - tuple(reversed(parts)).index(SESSION_DIR_NAME)
        )
    except ValueError:
        return None
    role_index = session_root_index + 1
    if role_index >= len(parts):
        return None
    try:
        role = AgentRole(parts[role_index])
    except ValueError:
        return None
    namespace = parts[role_index + 1] if role_index + 1 < len(parts) else ""
    worktree = Path(*parts[:session_root_index])
    return worktree, role, namespace


def _force_remove_readonly(
    func: Callable[..., object], path: str, _exc_info: object
) -> None:
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def is_stage_done_for(worktree: Path, role: AgentRole) -> bool:
    return RoleSession(worktree, role).is_done()


def any_role_dir_present(worktree_path: Path) -> bool:
    session_base = worktree_path / SESSION_DIR_NAME
    if not session_base.is_dir():
        return False
    return any(candidate.is_dir() for candidate in session_base.iterdir())


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


class RoleSession:
    def __init__(self, worktree: Path, role: AgentRole, namespace: str = "") -> None:
        self._worktree = worktree
        self._role = role
        self._namespace = namespace

    @property
    def path(self) -> Path:
        base = self._worktree / SESSION_DIR_NAME / self._role.value
        return base / self._namespace if self._namespace else base

    def _continuation_path(self) -> Path:
        return self.path / _CONTINUATION_FILENAME

    def _done_path(self) -> Path:
        return self.path / _DONE_FILENAME

    def _fingerprint_path(self) -> Path:
        return self.path / _FINGERPRINT_FILENAME

    @staticmethod
    def provider_state_relpath_for(
        role: AgentRole,
        provider_name: str,
        namespace: str = "",
    ) -> str:
        return provider_state_relpath(role, provider_name, namespace)

    def provider_state_relpath(self, provider_name: str) -> str:
        return self.provider_state_relpath_for(
            self._role,
            provider_name,
            self._namespace,
        ).rstrip("/")

    def provider_state_dir(self, provider_name: str) -> Path:
        return self._worktree / self.provider_state_relpath(provider_name)

    def read_fingerprint(self) -> str | None:
        p = self._fingerprint_path()
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8")

    def write_fingerprint(self, fingerprint: str) -> None:
        p = self._fingerprint_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(fingerprint, encoding="utf-8")

    def write_continuation(self, serialized: str) -> None:
        path = self._continuation_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")

    def read_continuation(self) -> str:
        return self._continuation_path().read_text(encoding="utf-8")

    def is_resumable(self) -> bool:
        return self._continuation_path().is_file()

    def is_done(self) -> bool:
        return self._done_path().is_file()

    def run_kind(self) -> RunKind:
        return RunKind.RESUME if self.is_resumable() else RunKind.FRESH

    def start_fresh(self) -> None:
        if self.path.is_dir():
            shutil.rmtree(self.path, onerror=_force_remove_readonly)
        self.path.mkdir(parents=True, exist_ok=True)

    def clear_provider_state_and_signal_completion(self) -> None:
        if not self.path.is_dir():
            return
        for child in self.path.iterdir():
            if is_service_session_metadata_path(child):
                continue
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, onerror=_force_remove_readonly)
        self._done_path().write_text("", encoding="utf-8")

    def discard(self) -> None:
        if self.path.is_dir():
            shutil.rmtree(self.path, onerror=_force_remove_readonly)
