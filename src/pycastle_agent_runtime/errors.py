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
        from pycastle.errors import (
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
