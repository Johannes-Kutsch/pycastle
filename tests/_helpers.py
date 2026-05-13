from pathlib import Path

from pycastle.agent_output_protocol import AgentRole
from pycastle.errors import AgentFailedError


def make_agent_failed_error(
    role: AgentRole,
    worktree_path: Path,
    *,
    failure_class: str = "",
    namespace: str = "",
) -> AgentFailedError:
    return AgentFailedError(
        role_value=role.value,
        worktree_path=worktree_path,
        namespace=namespace,
        failure_class=failure_class,
    )
