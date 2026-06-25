import sys


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
        pycastle_errors = sys.modules.get("pycastle.errors")
        if pycastle_errors is not None:
            return getattr(pycastle_errors, name)
        from . import error_types

        return {
            "AgentCredentialFailureError": error_types.AgentCredentialFailureError,
            "AgentFailedError": error_types.AgentFailedError,
            "AgentTimeoutError": error_types.AgentTimeoutError,
            "HardAgentError": error_types.HardAgentError,
            "RuntimeConfigurationError": error_types.RuntimeConfigurationError,
            "TransientAgentError": error_types.TransientAgentError,
            "UsageLimitError": error_types.UsageLimitError,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
