import uuid
from enum import Enum
from pathlib import Path

from .agent_output_protocol import AgentRole

_NAMESPACE = uuid.NAMESPACE_DNS


class RunKind(Enum):
    FRESH = "fresh"
    RESUME = "resume"


def has_resumable_session(role_dir: Path) -> bool:
    return role_dir.is_dir() and any(f for f in role_dir.rglob("*") if f.is_file())


def decide_agent_run_kind(role: AgentRole, *, session_dir_present: bool) -> RunKind:
    return RunKind.RESUME if session_dir_present else RunKind.FRESH


def derived_session_uuid(role: AgentRole, worktree_path: Path) -> str:
    role_ns = uuid.uuid5(_NAMESPACE, f"pycastle.{role.value}")
    session_id = uuid.uuid5(role_ns, str(worktree_path.resolve()))
    return str(session_id)
