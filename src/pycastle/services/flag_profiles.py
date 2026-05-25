from __future__ import annotations

import dataclasses

from ..agents.output_protocol import AgentRole


@dataclasses.dataclass(frozen=True)
class FlagProfile:
    tools: str | None = None
    disallowed_tools: tuple[str, ...] = ()
    strict_mcp: bool = False


_READ_ONLY_TOOLS = ("Edit", "Write", "NotebookEdit")

_READ_ONLY_ROLES = frozenset({AgentRole.PREFLIGHT_ISSUE, AgentRole.IMPROVE})


def flag_profile_for(role: AgentRole) -> FlagProfile:
    if role == AgentRole.PLANNER:
        return FlagProfile(tools="Read,Glob", strict_mcp=True)
    if role is AgentRole.DIVERGENCE_RESOLVER:
        return FlagProfile(tools="Read,Edit,Bash", strict_mcp=True)
    if role in _READ_ONLY_ROLES:
        return FlagProfile(disallowed_tools=_READ_ONLY_TOOLS, strict_mcp=True)
    return FlagProfile(strict_mcp=True)
