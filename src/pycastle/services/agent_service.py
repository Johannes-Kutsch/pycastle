from typing import Protocol

from pycastle_agent_runtime.contracts import (
    AgentService as RuntimeAgentService,
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
from pycastle_agent_runtime.provider_session_adapter import ProviderSessionService


class AgentService(RuntimeAgentService, ProviderSessionService, Protocol):
    pass


__all__ = [
    "AgentService",
    "AssistantTurn",
    "CredentialFailure",
    "HardError",
    "ParsedTurn",
    "PromptTokens",
    "Result",
    "TransientError",
    "UnsupportedTokens",
    "UsageLimit",
]
