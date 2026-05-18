import shutil
import uuid
from enum import Enum
from pathlib import Path

from .agents.output_protocol import AgentRole

_NAMESPACE = uuid.NAMESPACE_DNS

SESSION_DIR_NAME = ".pycastle-session"


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

    def is_resumable(self) -> bool:
        return self.path.is_dir() and any(f.is_file() for f in self.path.rglob("*"))

    def is_done(self) -> bool:
        return self.path.is_dir() and not self.is_resumable()

    def run_kind(self) -> RunKind:
        return RunKind.RESUME if self.is_resumable() else RunKind.FRESH

    def start_fresh(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
        self.path.mkdir(parents=True, exist_ok=True)

    def mark_done(self) -> None:
        if not self.path.is_dir():
            return
        for child in self.path.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

    def discard(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
