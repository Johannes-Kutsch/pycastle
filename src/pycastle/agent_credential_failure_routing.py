from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from .bug_reporter import file_agent_credential_failure_issue
from .errors import AgentCredentialFailureError, HardAgentError

if TYPE_CHECKING:
    from .services import GithubService

_SHARED_AGENT_CREDENTIAL_FAILURE_CLASSIFICATION = (
    "operator_actionable_agent_credential_failure"
)


@dataclasses.dataclass(frozen=True)
class AgentCredentialFailureRouteResult:
    status_code: int | None
    status_message: str
    issue_url: str | None


def _is_codex_refresh_token_reused_signature(text: str) -> bool:
    if "refresh_token_reused" in text:
        return True
    lowered = text.lower()
    return (
        "access token could not be refreshed" in lowered
        and "refresh token was already used" in lowered
    )


def _is_codex_missing_host_auth_signature(text: str) -> bool:
    lowered = text.lower()
    return (
        "codex authentication missing" in lowered
        and "codex login" in lowered
        and "host" in lowered
    )


def _is_claude_subscription_access_denial(text: str) -> bool:
    return "disabled claude subscription access for claude code" in text.lower()


def _is_opencode_invalid_api_key_signature(text: str) -> bool:
    lowered = text.lower()
    return "invalid api key" in lowered or "invalid_api_key" in lowered


def _shared_credential_failure_remediation(
    *,
    service_name: str,
    raw: str,
    rendered_observations: tuple[tuple[str, str], ...],
) -> str:
    haystacks = tuple(text for _, text in rendered_observations) + (raw,)
    if service_name == "codex":
        if any(_is_codex_refresh_token_reused_signature(text) for text in haystacks):
            return "Run `codex login` on the host to reseed credentials."
        if any(_is_codex_missing_host_auth_signature(text) for text in haystacks):
            return "Run `codex login` on the host to seed Codex credentials before dispatch."
    if service_name == "claude" and any(
        _is_claude_subscription_access_denial(text) for text in haystacks
    ):
        return (
            "Restore Claude Code subscription access or use a token/account with "
            "access and rerun pycastle."
        )
    if service_name == "opencode" and any(
        _is_opencode_invalid_api_key_signature(text) for text in haystacks
    ):
        return "Update the configured OpenCode API key and rerun pycastle."
    return "Repair the local agent credentials/account access."


def _classify_agent_credential_failure(
    *,
    service_name: str,
    status_code: int | None,
    classification: str | None,
    raw: str,
    observations: tuple,
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    if classification == _SHARED_AGENT_CREDENTIAL_FAILURE_CLASSIFICATION:
        rendered = tuple(
            (obs.source_stream, obs.raw_provider_text) for obs in observations
        ) or (("raw error", raw),)
        return (
            _shared_credential_failure_remediation(
                service_name=service_name,
                raw=raw,
                rendered_observations=rendered,
            ),
            rendered,
        )

    rendered_observations = tuple(
        (obs.source_stream, obs.raw_provider_text) for obs in observations
    )
    haystacks = tuple(text for _, text in rendered_observations) + (raw,)
    if service_name == "claude" and status_code == 403:
        if any(_is_claude_subscription_access_denial(text) for text in haystacks):
            return (
                "Restore Claude Code subscription access or switch to a token with access.",
                rendered_observations or (("raw error", raw),),
            )
        return None
    if service_name != "codex" or status_code != 401:
        return None

    if any(_is_codex_refresh_token_reused_signature(text) for text in haystacks):
        return (
            "Run `codex login` on the host to reseed credentials.",
            rendered_observations or (("raw error", raw),),
        )
    if any(_is_codex_missing_host_auth_signature(text) for text in haystacks):
        return (
            "Run `codex login` on the host to seed Codex credentials before dispatch.",
            rendered_observations or (("raw error", raw),),
        )
    return None


def route_agent_credential_failure(
    *,
    provider_failure: HardAgentError,
    github_svc: "GithubService",
) -> AgentCredentialFailureRouteResult | None:
    raw = provider_failure.args[0] if provider_failure.args else ""
    service_name = getattr(provider_failure, "service_name", "claude") or "claude"
    credential_failure = _classify_agent_credential_failure(
        service_name=service_name,
        status_code=provider_failure.status_code,
        classification=getattr(provider_failure, "classification", None),
        raw=raw,
        observations=getattr(provider_failure, "observations", ()),
    )
    if credential_failure is None:
        if not isinstance(provider_failure, AgentCredentialFailureError):
            return None
        credential_failure = (
            "Repair the local agent credentials/account access.",
            tuple(
                (obs.source_stream, obs.raw_provider_text)
                for obs in getattr(provider_failure, "observations", ())
            )
            or (("raw error", raw),),
        )

    remediation, rendered_observations = credential_failure
    issue_url = file_agent_credential_failure_issue(
        service_name=service_name,
        role_name=provider_failure.caller,
        status_code=provider_failure.status_code,
        raw_result_envelope=raw,
        remediation=remediation,
        observations=rendered_observations,
        github_svc=github_svc,
    )
    status_code_str = (
        str(provider_failure.status_code)
        if provider_failure.status_code is not None
        else "no status"
    )
    status_message = (
        f"operator-actionable agent credential failure: status {status_code_str}"
    )
    if issue_url is None:
        local_evidence = rendered_observations[0][1] if rendered_observations else raw
        status_message = (
            "operator-actionable agent credential failure: "
            f"{remediation} Evidence: {local_evidence}"
        )
    return AgentCredentialFailureRouteResult(
        status_code=provider_failure.status_code,
        status_message=status_message,
        issue_url=issue_url,
    )
