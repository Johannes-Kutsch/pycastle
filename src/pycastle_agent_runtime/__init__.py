from __future__ import annotations

from typing import TYPE_CHECKING

from pycastle.config.types import StageOverride
from pycastle.services.agent_service import (
    AgentService,
    AssistantTurn,
    CredentialFailure,
    HardError,
    ParsedTurn,
    PromptTokens,
    Result,
    TransientError,
    UnsupportedTokens,
    UsageLimit,
)

if TYPE_CHECKING:
    from pycastle.agents.runner import AgentRunner, AgentRunnerProtocol, RunRequest
    from pycastle_agent_runtime.runtime import (
        PromptRunRequest,
        PromptRuntime,
        ToolPolicy,
    )
    from pycastle_agent_runtime.service_registry import ServiceRegistry
    from pycastle_agent_runtime.stage_priority_chain import (
        ChainEntry,
        ConfiguredCandidateChain,
        ConfiguredCandidateSelection,
    )

__all__ = [
    "AgentRunner",
    "AgentRunnerProtocol",
    "AgentService",
    "AssistantTurn",
    "ChainEntry",
    "ConfiguredCandidateChain",
    "ConfiguredCandidateSelection",
    "CredentialFailure",
    "HardError",
    "ParsedTurn",
    "PromptRunRequest",
    "PromptRuntime",
    "PromptTokens",
    "Result",
    "RunRequest",
    "ServiceRegistry",
    "StageOverride",
    "chain_entries",
    "configured_candidate_chain",
    "iter_stage_chain",
    "referenced_service_names",
    "render_chain_label",
    "select_configured_candidate_chain",
    "ToolPolicy",
    "TransientError",
    "UnsupportedTokens",
    "UsageLimit",
    "validation_labels",
    "run_prompt",
]


def __getattr__(name: str):
    if name in {"AgentRunner", "AgentRunnerProtocol", "RunRequest"}:
        from pycastle.agents import runner

        return getattr(runner, name)
    if name in {"PromptRunRequest", "PromptRuntime", "ToolPolicy", "run_prompt"}:
        from pycastle_agent_runtime import runtime

        return getattr(runtime, name)
    if name == "ServiceRegistry":
        from pycastle_agent_runtime.service_registry import ServiceRegistry

        return ServiceRegistry
    if name in {
        "ChainEntry",
        "ConfiguredCandidateChain",
        "ConfiguredCandidateSelection",
        "chain_entries",
        "configured_candidate_chain",
        "iter_stage_chain",
        "referenced_service_names",
        "render_chain_label",
        "select_configured_candidate_chain",
        "validation_labels",
    }:
        from pycastle_agent_runtime import stage_priority_chain

        return getattr(stage_priority_chain, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
