from __future__ import annotations

from typing import TYPE_CHECKING

from pycastle.infrastructure.agent_invocation_log import (
    AgentInvocationLog,
    LogicalAgentInvocationLog,
    WorkInvocationLog,
)
from pycastle.services.agent_service import (
    AgentService,
    AssistantTurn,
    CredentialFailure,
    HardError,
    ParsedTurn,
    PromptTokens,
    ProviderSessionRecordingStore,
    ProviderStatePreparationAction,
    Result,
    ToolPolicy,
    TransientError,
    UnsupportedTokens,
    UsageLimit,
)
from .execution_contracts import (
    RunSessionPlan,
    TextOutputAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
)
from .provider_session_adapter import ProviderSessionAdapter
from pycastle.agents.output_protocol import AgentRole
from .session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
    RunKind,
)
from pycastle.config.types import StageOverride

if TYPE_CHECKING:
    from pycastle_agent_runtime.session_planning import (
        ProviderRunStatePlan,
        ProviderRunStatePlanRequest,
        ResidentSessionPlan,
        ResidentSessionPlanRequest,
        plan_provider_run_state,
        plan_resident_session,
    )
    from pycastle.services.service_registry import ServiceRegistry
    from pycastle.stage_priority_chain import (
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
    from pycastle_agent_runtime.work import (
        CancellationToken,
        invoke_work,
    )

__all__ = [
    "AgentInvocationLog",
    "AgentService",
    "AgentRole",
    "AssistantTurn",
    "CancellationToken",
    "ChainEntry",
    "ConfiguredCandidateChain",
    "ConfiguredCandidateSelection",
    "CredentialFailure",
    "HardError",
    "LogicalAgentInvocationLog",
    "ParsedTurn",
    "ProviderSessionAdapter",
    "ProviderSessionPreferences",
    "ProviderSessionPreferencesRequest",
    "ProviderSessionRecordingStore",
    "ProviderSessionState",
    "ProviderSessionStateRequest",
    "ProviderStatePreparationAction",
    "PromptTokens",
    "PromptRunRequest",
    "PromptRunSession",
    "PromptRuntime",
    "PromptRuntimeExecutionAdapter",
    "Result",
    "RunSessionPlan",
    "RunKind",
    "ProviderRunStatePlan",
    "ProviderRunStatePlanRequest",
    "ResidentSessionPlan",
    "ResidentSessionPlanRequest",
    "ServiceRegistry",
    "SleepUntil",
    "Stop",
    "StageOverride",
    "TextOutputAdapter",
    "OneShotRunRequest",
    "OneShotRunResult",
    "OneShotRuntime",
    "OneShotRuntimeExecutionAdapter",
    "OneShotRuntimeMetadata",
    "ResidentRunRequest",
    "ResidentRunResult",
    "ResidentRuntime",
    "ResidentRuntimeExecutionAdapter",
    "ResidentRuntimeMetadata",
    "chain_entries",
    "ContinueNow",
    "configured_candidate_chain",
    "invoke_work",
    "iter_stage_chain",
    "plan_provider_run_state",
    "referenced_service_names",
    "render_chain_label",
    "select_configured_candidate_chain",
    "plan_resident_session",
    "UsageLimitContinuationDecision",
    "UsageLimitOutcome",
    "ToolPolicy",
    "TransientError",
    "UnsupportedTokens",
    "UsageLimit",
    "validation_labels",
    "decide_usage_limit_continuation",
    "WorkInvocationDependencies",
    "WorkInvocationRequest",
    "WorkInvocationLog",
    "WorktreeMount",
    "run_one_shot",
    "run_prompt",
    "run_resident_prompt",
]


def __getattr__(name: str):
    if name in {
        "AgentCredentialFailureError",
        "AgentFailedError",
        "AgentTimeoutError",
        "HardAgentError",
        "RuntimeConfigurationError",
        "TransientAgentError",
        "UsageLimitError",
    }:
        from .errors import (
            AgentCredentialFailureError,
            AgentFailedError,
            AgentTimeoutError,
            HardAgentError,
            RuntimeConfigurationError,
            TransientAgentError,
            UsageLimitError,
        )

        return {
            "AgentCredentialFailureError": AgentCredentialFailureError,
            "AgentFailedError": AgentFailedError,
            "AgentTimeoutError": AgentTimeoutError,
            "HardAgentError": HardAgentError,
            "RuntimeConfigurationError": RuntimeConfigurationError,
            "TransientAgentError": TransientAgentError,
            "UsageLimitError": UsageLimitError,
        }[name]
    if name == "ProviderErrorObservation":
        from .provider_errors import ProviderErrorObservation

        return ProviderErrorObservation
    if name in {
        "OneShotRunRequest",
        "OneShotRunResult",
        "OneShotRuntime",
        "OneShotRuntimeExecutionAdapter",
        "OneShotRuntimeMetadata",
        "ResidentRunRequest",
        "ResidentRunResult",
        "ResidentRuntime",
        "ResidentRuntimeExecutionAdapter",
        "ResidentRuntimeMetadata",
        "PromptRunRequest",
        "PromptRunSession",
        "PromptRuntimeExecutionAdapter",
        "PromptRuntime",
        "WorktreeMount",
        "run_one_shot",
        "run_prompt",
        "run_resident_prompt",
    }:
        if name in {
            "OneShotRunRequest",
            "OneShotRunResult",
            "OneShotRuntime",
            "OneShotRuntimeExecutionAdapter",
            "OneShotRuntimeMetadata",
            "ResidentRunRequest",
            "ResidentRunResult",
            "ResidentRuntime",
            "ResidentRuntimeExecutionAdapter",
            "ResidentRuntimeMetadata",
            "PromptRuntime",
            "run_one_shot",
            "run_prompt",
            "run_resident_prompt",
        }:
            from pycastle_agent_runtime import runtime

            return getattr(runtime, name)
        from pycastle_agent_runtime import execution_contracts

        return getattr(execution_contracts, name)
    if name == "ServiceRegistry":
        from pycastle.services.service_registry import ServiceRegistry

        return ServiceRegistry
    if name in {
        "CancellationToken",
        "PreparedProviderRunSession",
        "PreparedSession",
        "PrepareSessionAdapter",
        "RunSessionPlan",
        "SetupFailureTranslator",
        "StatusDisplayFactory",
        "StatusRowFactory",
        "TextOutputAdapter",
        "WorkExecutionAdapter",
        "WorkInvocationDependencies",
        "WorkInvocationRequest",
        "WorkModelDisplayMetadata",
        "WorkOutputAdapter",
        "WorkStatusDisplay",
        "WorkStatusRow",
        "invoke_work",
    }:
        if name == "invoke_work" or name == "CancellationToken":
            from pycastle_agent_runtime import work

            return getattr(work, name)
        from pycastle_agent_runtime import execution_contracts

        return getattr(execution_contracts, name)
    if name in {
        "ProviderRunStatePlan",
        "ProviderRunStatePlanRequest",
        "ResidentSessionPlan",
        "ResidentSessionPlanRequest",
        "plan_provider_run_state",
        "plan_resident_session",
    }:
        from pycastle_agent_runtime import session_planning

        return getattr(session_planning, name)
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
        from pycastle import stage_priority_chain

        return getattr(stage_priority_chain, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
