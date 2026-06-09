from __future__ import annotations

from typing import TYPE_CHECKING

from ._import_isolation import assert_runtime_import_isolation

from .contracts import (
    AgentService,
    AssistantTurn,
    CredentialFailure,
    HardError,
    ParsedTurn,
    PromptTokens,
    Result,
    ToolPolicy,
    TransientError,
    UnsupportedTokens,
    UsageLimit,
)
from .errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    HardAgentError,
    PycastleError,
    TransientAgentError,
    UsageLimitError,
)
from .provider_errors import ProviderErrorObservation
from .roles import AgentRole
from .session import ProviderSessionState, ProviderSessionStateRequest, RunKind
from .types import StageOverride

if TYPE_CHECKING:
    from pycastle_agent_runtime.service_registry import ServiceRegistry
    from pycastle_agent_runtime.stage_priority_chain import (
        ChainEntry,
        ConfiguredCandidateChain,
        ConfiguredCandidateSelection,
    )
    from pycastle_agent_runtime.usage_limit_decision import (
        ContinueNow,
        SleepUntil,
        Stop,
        UsageLimitContinuationDecision,
        UsageLimitOutcome,
    )

__all__ = [
    "AgentCredentialFailureError",
    "AgentFailedError",
    "AgentService",
    "AgentRole",
    "AgentTimeoutError",
    "AssistantTurn",
    "ChainEntry",
    "ConfiguredCandidateChain",
    "ConfiguredCandidateSelection",
    "CredentialFailure",
    "HardError",
    "HardAgentError",
    "ParsedTurn",
    "ProviderErrorObservation",
    "ProviderSessionState",
    "ProviderSessionStateRequest",
    "PromptTokens",
    "PycastleError",
    "Result",
    "RunKind",
    "ServiceRegistry",
    "SleepUntil",
    "Stop",
    "StageOverride",
    "chain_entries",
    "ContinueNow",
    "configured_candidate_chain",
    "iter_stage_chain",
    "referenced_service_names",
    "render_chain_label",
    "select_configured_candidate_chain",
    "UsageLimitContinuationDecision",
    "UsageLimitOutcome",
    "ToolPolicy",
    "TransientError",
    "TransientAgentError",
    "UnsupportedTokens",
    "UsageLimit",
    "UsageLimitError",
    "validation_labels",
    "decide_usage_limit_continuation",
]


def __getattr__(name: str):
    if name in {"AgentRunner", "AgentRunnerProtocol", "RunRequest"}:
        from pycastle.agents import runner

        return getattr(runner, name)
    if name == "run":
        from pycastle_agent_runtime.orchestration import run

        return run
    if name in {
        "PromptRunRequest",
        "PromptRunSession",
        "PromptRuntime",
        "WorktreeMount",
        "run_prompt",
    }:
        from pycastle_agent_runtime import runtime

        return getattr(runtime, name)
    if name == "ServiceRegistry":
        from pycastle_agent_runtime.service_registry import ServiceRegistry

        return ServiceRegistry
    if name in {
        "ContinueNow",
        "SleepUntil",
        "Stop",
        "UsageLimitContinuationDecision",
        "UsageLimitOutcome",
        "decide_usage_limit_continuation",
    }:
        from pycastle_agent_runtime import usage_limit_decision

        return getattr(usage_limit_decision, name)
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


assert_runtime_import_isolation(importer=__name__)
