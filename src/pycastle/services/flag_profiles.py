from __future__ import annotations

import dataclasses
import enum

from ..agents.output_protocol import AgentRole


@dataclasses.dataclass(frozen=True)
class FlagProfile:
    tools: str | None = None
    disallowed_tools: tuple[str, ...] = ()
    strict_mcp: bool = False


class AgentToolPolicyGroup(enum.Enum):
    RESTRICTED = "restricted"
    PARTIAL = "partial"
    FULL = "full"


_READ_ONLY_TOOLS = ("Edit", "Write", "NotebookEdit")

_ROLE_TOOL_POLICY_GROUPS = {
    AgentRole.PLANNER: AgentToolPolicyGroup.RESTRICTED,
    AgentRole.PREFLIGHT_ISSUE: AgentToolPolicyGroup.PARTIAL,
    AgentRole.IMPLEMENTER: AgentToolPolicyGroup.FULL,
    AgentRole.REVIEWER: AgentToolPolicyGroup.FULL,
    AgentRole.MERGER: AgentToolPolicyGroup.FULL,
    AgentRole.IMPROVE: AgentToolPolicyGroup.PARTIAL,
    AgentRole.FAILURE_REPORT: AgentToolPolicyGroup.PARTIAL,
    AgentRole.DIVERGENCE_RESOLVER: AgentToolPolicyGroup.PARTIAL,
}

assert len(_ROLE_TOOL_POLICY_GROUPS) == len(AgentRole)

_FLAG_PROFILES_BY_POLICY_GROUP = {
    AgentToolPolicyGroup.RESTRICTED: FlagProfile(tools="Read,Glob", strict_mcp=True),
    AgentToolPolicyGroup.PARTIAL: FlagProfile(
        disallowed_tools=_READ_ONLY_TOOLS,
        strict_mcp=True,
    ),
    AgentToolPolicyGroup.FULL: FlagProfile(strict_mcp=True),
}


def tool_policy_group_for(role: AgentRole) -> AgentToolPolicyGroup:
    return _ROLE_TOOL_POLICY_GROUPS[role]


def flag_profile_for(role: AgentRole) -> FlagProfile:
    return _FLAG_PROFILES_BY_POLICY_GROUP[tool_policy_group_for(role)]


def flag_profile_for_tool_policy(tool_policy: AgentToolPolicyGroup) -> FlagProfile:
    return _FLAG_PROFILES_BY_POLICY_GROUP[tool_policy]
