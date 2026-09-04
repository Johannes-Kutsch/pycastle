from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

from agent_runtime.errors import AgentCredentialFailureError, HardAgentError

from pycastle.upstream_issue_report import (
    BUG_AND_TRIAGE_LABELS,
    UpstreamIssueReport,
    file_upstream_issue,
)
from pycastle.upstream_issue_report import (
    agent_credential_failure_body as _upstream_agent_credential_failure_body,
)

if TYPE_CHECKING:
    from pycastle.services import GithubService

_SHARED_AGENT_CREDENTIAL_FAILURE_CLASSIFICATION = (
    "operator_actionable_agent_credential_failure"
)
_CODEX_AUTH_LINEAGE_EXHAUSTED_CLASSIFICATION = "codex_auth_lineage_exhausted"
_AGENT_CREDENTIAL_FAILURE_TITLE = (
    "[pycastle] operator-actionable agent credential failure"
)
_CREDENTIAL_KEY_RE = (
    r"(?:api(?:[_ -]?|)key|access(?:[_ -]?|)token|refresh(?:[_ -]?|)token|"
    r"token|secret|password)"
)
_CREDENTIAL_NAMED_VALUE_RE = re.compile(
    rf'(?i)(["\']?{_CREDENTIAL_KEY_RE}["\']?\s*[:=]\s*)(["\']?)([^"\'\s,;}}]+)(\2)'
)
_CREDENTIAL_AFTER_LABEL_RE = re.compile(
    r"(?i)\b(access token|refresh token|api key|token|secret|password)\s+([A-Za-z0-9._:-]{8,})"
)
_SK_STYLE_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


@dataclasses.dataclass(frozen=True)
class AgentCredentialFailureRouteResult:
    status_code: int | None
    status_message: str
    issue_url: str | None


@dataclasses.dataclass(frozen=True)
class _CredentialFailureIssueLookupResult:
    issue_url: str | None
    reused_issue_number: int | None = None


@dataclasses.dataclass(frozen=True)
class _CredentialFailureInterpretation:
    remediation: str
    rendered_observations: tuple[tuple[str, str], ...]


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


def _render_observations(
    raw: str,
    observations: tuple,
) -> tuple[tuple[str, str], ...]:
    rendered: list[tuple[str, str]] = []
    for observation in observations:
        if (
            isinstance(observation, tuple)
            and len(observation) == 2  # noqa: PLR2004  # 2-element (stream, text) tuple
            and isinstance(observation[0], str)
            and isinstance(observation[1], str)
        ):
            rendered.append((observation[0], observation[1]))
            continue
        source_stream = getattr(observation, "source_stream", None)
        raw_text = getattr(observation, "raw_provider_text", None)
        if isinstance(source_stream, str) and isinstance(raw_text, str):
            rendered.append((source_stream, raw_text))
    return tuple(rendered) or (("raw error", raw),)


def _redact_credential_material(text: str) -> str:
    redacted = _CREDENTIAL_NAMED_VALUE_RE.sub(r"\1\2[REDACTED]\4", text)
    redacted = _CREDENTIAL_AFTER_LABEL_RE.sub(r"\1 [REDACTED]", redacted)
    return _SK_STYLE_TOKEN_RE.sub("[REDACTED]", redacted)


def _file_or_reuse_agent_credential_failure_issue(
    *,
    service_name: str,
    role_name: str,
    status_code: int | None,
    raw_result_envelope: str,
    remediation: str,
    observations: tuple[tuple[str, str], ...],
    github_svc: GithubService,
) -> _CredentialFailureIssueLookupResult:
    existing = github_svc.search_open_issues_by_title(_AGENT_CREDENTIAL_FAILURE_TITLE)
    if existing:
        return _CredentialFailureIssueLookupResult(
            issue_url=f"https://github.com/{github_svc.repo}/issues/{existing[0]}",
            reused_issue_number=existing[0],
        )

    redacted_observations = tuple(
        (source_stream, _redact_credential_material(raw_text))
        for source_stream, raw_text in observations
    )
    body = _upstream_agent_credential_failure_body(
        service_name=service_name,
        role_name=role_name,
        status_code=status_code,
        raw_result_envelope=_redact_credential_material(raw_result_envelope),
        remediation=remediation,
        observations=redacted_observations,
    )
    number = file_upstream_issue(
        UpstreamIssueReport(
            dedupe_key=_AGENT_CREDENTIAL_FAILURE_TITLE,
            title=_AGENT_CREDENTIAL_FAILURE_TITLE,
            body=body,
            labels=BUG_AND_TRIAGE_LABELS,
            github_svc=github_svc,
        )
    )
    if number is None:
        return _CredentialFailureIssueLookupResult(issue_url=None)
    url = f"https://github.com/{github_svc.repo}/issues/{number}"
    return _CredentialFailureIssueLookupResult(issue_url=url)


def _build_local_fallback_status_message(
    *,
    raw: str,
    interpretation: _CredentialFailureInterpretation,
) -> str:
    local_evidence = (
        interpretation.rendered_observations[0][1]
        if interpretation.rendered_observations
        else raw
    )
    redacted_local_evidence = _redact_credential_material(local_evidence)
    return (
        "operator-actionable agent credential failure: "
        f"{interpretation.remediation} Evidence: {redacted_local_evidence}"
    )


def _select_remediation(
    *,
    service_name: str,
    classification: str | None = None,
    raw: str,
    rendered_observations: tuple[tuple[str, str], ...],
) -> str:
    if classification == _CODEX_AUTH_LINEAGE_EXHAUSTED_CLASSIFICATION:
        return "Run `codex login` on the host to reseed credentials."
    if classification == _SHARED_AGENT_CREDENTIAL_FAILURE_CLASSIFICATION:
        if service_name == "claude":
            return (
                "Restore Claude Code subscription access or use a token/account with "
                "access and rerun pycastle."
            )
        if service_name == "opencode":
            return "Update the configured OpenCode API key and rerun pycastle."

    haystacks = (*tuple(text for _, text in rendered_observations), raw)
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


def _interpret_agent_credential_failure(
    *,
    service_name: str,
    classification: str | None,
    raw: str,
    observations: tuple,
) -> _CredentialFailureInterpretation | None:
    rendered_observations = _render_observations(raw, observations)
    haystacks = (*tuple(text for _, text in rendered_observations), raw)
    if classification == _CODEX_AUTH_LINEAGE_EXHAUSTED_CLASSIFICATION:
        return _CredentialFailureInterpretation(
            remediation=_select_remediation(
                service_name=service_name,
                classification=classification,
                raw=raw,
                rendered_observations=rendered_observations,
            ),
            rendered_observations=rendered_observations,
        )
    if classification == _SHARED_AGENT_CREDENTIAL_FAILURE_CLASSIFICATION:
        if service_name == "codex" and not (
            any(_is_codex_refresh_token_reused_signature(text) for text in haystacks)
            or any(_is_codex_missing_host_auth_signature(text) for text in haystacks)
        ):
            return None
        return _CredentialFailureInterpretation(
            remediation=_select_remediation(
                service_name=service_name,
                classification=classification,
                raw=raw,
                rendered_observations=rendered_observations,
            ),
            rendered_observations=rendered_observations,
        )

    if any(_is_codex_refresh_token_reused_signature(text) for text in haystacks):
        return _CredentialFailureInterpretation(
            remediation="Run `codex login` on the host to reseed credentials.",
            rendered_observations=rendered_observations,
        )
    if any(_is_codex_missing_host_auth_signature(text) for text in haystacks):
        return _CredentialFailureInterpretation(
            remediation=(
                "Run `codex login` on the host to seed Codex credentials before "
                "dispatch."
            ),
            rendered_observations=rendered_observations,
        )
    return None


def route_agent_credential_failure(
    *,
    provider_failure: HardAgentError,
    github_svc: GithubService,
) -> AgentCredentialFailureRouteResult | None:
    raw = provider_failure.args[0] if provider_failure.args else ""
    service_name = getattr(provider_failure, "service_name", "claude") or "claude"
    raw_observations = getattr(provider_failure, "source_observations", ())
    if not raw_observations:
        raw_observations = getattr(provider_failure, "observations", ())
    if not raw_observations and raw:
        if service_name == "codex" and '"code":"refresh_token_reused"' in raw:
            raw_observations = (
                ("stderr", '{"code":"refresh_token_reused"}'),
                (
                    "stderr",
                    (
                        "The access token could not be refreshed because "
                        "refreshToken=[REDACTED] was already used."
                    ),
                ),
                ("stderr", raw),
            )
        else:
            raw_observations = (("stderr", raw),)
    interpretation = _interpret_agent_credential_failure(
        service_name=service_name,
        classification=getattr(provider_failure, "classification", None),
        raw=raw,
        observations=raw_observations,
    )
    if interpretation is None:
        if service_name == "codex":
            return None
        if not isinstance(provider_failure, AgentCredentialFailureError):
            return None
        interpretation = _CredentialFailureInterpretation(
            remediation="Repair the local agent credentials/account access.",
            rendered_observations=_render_observations(raw, raw_observations),
        )

    _status_code = getattr(provider_failure, "status_code", None)
    issue_result = _file_or_reuse_agent_credential_failure_issue(
        service_name=service_name,
        role_name=provider_failure.caller,
        status_code=_status_code,
        raw_result_envelope=raw,
        remediation=interpretation.remediation,
        observations=interpretation.rendered_observations,
        github_svc=github_svc,
    )
    issue_url = issue_result.issue_url
    status_code_str = str(_status_code) if _status_code is not None else "no status"
    status_message = (
        f"operator-actionable agent credential failure: status {status_code_str}"
    )
    if issue_result.reused_issue_number is not None and issue_url is not None:
        status_message = (
            "operator-actionable agent credential failure: "
            f"reusing existing issue #{issue_result.reused_issue_number} "
            f"({issue_url})"
        )
    elif issue_url is None:
        status_message = _build_local_fallback_status_message(
            raw=raw,
            interpretation=interpretation,
        )
    return AgentCredentialFailureRouteResult(
        status_code=_status_code,
        status_message=status_message,
        issue_url=issue_url,
    )
