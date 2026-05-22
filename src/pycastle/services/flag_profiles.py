from __future__ import annotations

import dataclasses

from ..agents.output_protocol import AgentRole


@dataclasses.dataclass(frozen=True)
class FlagProfile:
    bare: bool = False
    tools: str | None = None
    disallowed_tools: tuple[str, ...] = ()
    strict_mcp: bool = False


def flag_profile_for(role: AgentRole) -> FlagProfile:
    return FlagProfile()
