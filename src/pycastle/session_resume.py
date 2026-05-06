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
