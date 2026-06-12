from pycastle_agent_runtime.provider_errors import ProviderErrorObservation
from pycastle_agent_runtime.session_planning import (
    AuthSeedingRequirement,
    LocalAuthSeedAction as RuntimeLocalAuthSeedAction,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
)

from ..errors import AgentCredentialFailureError


class LocalAuthSeedAction(RuntimeLocalAuthSeedAction):
    def require_source(self):
        if not self.source.exists():
            raise AgentCredentialFailureError(
                self.missing_source_message,
                status_code=401,
                service_name="codex",
                observations=(
                    ProviderErrorObservation(
                        service_name="codex",
                        raw_provider_text=self.missing_source_message,
                        source_stream="pre-dispatch host check",
                        status_code=401,
                    ),
                ),
            )
        return self.source


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "ProviderSessionDecision",
    "RecoveredSessionIdPersistence",
]
