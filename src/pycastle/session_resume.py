import shutil
import uuid
from enum import Enum
from pathlib import Path

from .agent_output_protocol import AgentRole

_NAMESPACE = uuid.NAMESPACE_DNS


class RunKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"


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


def any_role_has_session(worktree_path: Path) -> bool:
    session_base = worktree_path / ".pycastle-session"
    if not session_base.is_dir():
        return False
    return any(has_resumable_session(d) for d in session_base.iterdir() if d.is_dir())


def decide_agent_run_kind(role: AgentRole, *, session_dir_present: bool) -> RunKind:
    return RunKind.RESUME if session_dir_present else RunKind.FRESH


def derived_session_uuid(role: AgentRole, worktree_path: Path) -> str:
    role_ns = uuid.uuid5(_NAMESPACE, f"pycastle.{role.value}")
    session_id = uuid.uuid5(role_ns, str(worktree_path.resolve()))
    return str(session_id)
