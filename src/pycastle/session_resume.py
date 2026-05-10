import shutil
import uuid
from enum import Enum
from pathlib import Path

from .agent_output_protocol import AgentRole

_NAMESPACE = uuid.NAMESPACE_DNS

SESSION_DIR_NAME = ".pycastle-session"


class RunKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"


def session_dir_path(worktree: Path, role: AgentRole, namespace: str = "") -> Path:
    base = worktree / SESSION_DIR_NAME / role.value
    return base / namespace if namespace else base


def session_dir_rel(role: AgentRole, namespace: str = "") -> str:
    if namespace:
        return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/"
    return f"{SESSION_DIR_NAME}/{role.value}/"


def has_resumable_session(role_dir: Path) -> bool:
    return role_dir.is_dir() and any(f.is_file() for f in role_dir.rglob("*"))


def is_stage_done(role_dir: Path) -> bool:
    return role_dir.is_dir() and not has_resumable_session(role_dir)


def clear_session_dir(role_dir: Path) -> None:
    """Clear contents of a role session dir, leaving the dir as the stage-done signal."""
    if not role_dir.is_dir():
        return
    for child in role_dir.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def is_stage_done_for(worktree: Path, role: AgentRole) -> bool:
    return is_stage_done(session_dir_path(worktree, role))


def clear_stage(worktree: Path, role: AgentRole, namespace: str = "") -> None:
    clear_session_dir(session_dir_path(worktree, role, namespace))


def any_role_dir_present(worktree_path: Path) -> bool:
    session_base = worktree_path / SESSION_DIR_NAME
    if not session_base.is_dir():
        return False
    return any(d.is_dir() for d in session_base.iterdir())


def decide_agent_run_kind(role: AgentRole, *, session_dir_present: bool) -> RunKind:
    return RunKind.RESUME if session_dir_present else RunKind.FRESH


def derived_session_uuid(
    role: AgentRole, worktree_path: Path, session_namespace: str = ""
) -> str:
    role_key = (
        f"pycastle.{role.value}.{session_namespace}"
        if session_namespace
        else f"pycastle.{role.value}"
    )
    role_ns = uuid.uuid5(_NAMESPACE, role_key)
    session_id = uuid.uuid5(role_ns, str(worktree_path.resolve()))
    return str(session_id)


class RoleSession:
    def __init__(self, worktree: Path, role: AgentRole, namespace: str = "") -> None:
        self._worktree = worktree
        self._role = role
        self._namespace = namespace

    @property
    def path(self) -> Path:
        return session_dir_path(self._worktree, self._role, self._namespace)

    def claude_config_dir_relpath(self) -> str:
        return session_dir_rel(self._role, self._namespace)

    def session_uuid(self) -> str:
        return derived_session_uuid(self._role, self._worktree, self._namespace)

    def is_resumable(self) -> bool:
        return has_resumable_session(self.path)

    def is_done(self) -> bool:
        return is_stage_done(self.path)

    def run_kind(self) -> RunKind:
        return decide_agent_run_kind(
            self._role, session_dir_present=self.is_resumable()
        )

    def start_fresh(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
        self.path.mkdir(parents=True, exist_ok=True)

    def mark_done(self) -> None:
        clear_session_dir(self.path)
