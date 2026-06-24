from pycastle.provider_errors import ProviderErrorObservation
from pycastle_agent_runtime.session_planning import (
    AuthSeedingRequirement,
    LocalAuthSeedAction as RuntimeLocalAuthSeedAction,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
)

from ..errors import AgentCredentialFailureError


class LocalAuthSeedAction(RuntimeLocalAuthSeedAction):
    def require_source(self):
        if self.source.exists():
            return self.source
        message = self.missing_source_message
        service_name = self.missing_source_service_name
        status_code = self.missing_source_status_code
        classification = self.missing_source_classification
        observations = self.missing_source_observations
        if message is None or service_name is None:
            message = "Codex authentication missing: run `codex login` on the host."
            service_name = "codex"
            status_code = 401
            observations = (
                ProviderErrorObservation(
                    service_name=service_name,
                    raw_provider_text=message,
                    source_stream="pre-dispatch host check",
                    status_code=status_code,
                ),
            )
        raise AgentCredentialFailureError(
            message,
            status_code=status_code,
            service_name=service_name,
            classification=classification,
            observations=observations,
        )


__all__ = [
    "AuthSeedingRequirement",
    "LocalAuthSeedAction",
    "ProviderSessionDecision",
    "RecoveredSessionIdPersistence",
]
