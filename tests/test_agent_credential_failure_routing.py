from unittest.mock import MagicMock

from pycastle.agent_credential_failure_routing import (
    AgentCredentialFailureRouteResult,
    route_agent_credential_failure,
)
from pycastle.errors import AgentCredentialFailureError, HardAgentError
from pycastle.provider_errors import ProviderErrorObservation
from pycastle.services import GithubService


def test_route_agent_credential_failure_returns_terminal_route_result_for_shared_classification():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42

    err = AgentCredentialFailureError(
        message="OpenCode request failed: 401 invalid API key for provider opencode-go",
        status_code=401,
        service_name="opencode",
        classification="operator_actionable_agent_credential_failure",
        observations=(
            ProviderErrorObservation(
                service_name="opencode",
                raw_provider_text=(
                    "OpenCode request failed: 401 invalid API key for provider "
                    "opencode-go"
                ),
                source_stream="json_event.error",
                status_code=401,
            ),
        ),
    )
    err.caller = "Implementer"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result == AgentCredentialFailureRouteResult(
        status_code=401,
        status_message="operator-actionable agent credential failure: status 401",
        issue_url="https://github.com/owner/consuming-project/issues/42",
    )


def test_route_agent_credential_failure_interprets_codex_auth_lineage_exhaustion_in_routing_module():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42

    err = AgentCredentialFailureError(
        message=(
            'Error: API request failed: 401 Unauthorized: {"type":"error",'
            '"code":"refresh_token_reused","message":"This refresh token has already '
            'been used."}'
        ),
        status_code=401,
        service_name="codex",
        classification="codex_auth_lineage_exhausted",
        observations=(
            ProviderErrorObservation(
                service_name="codex",
                raw_provider_text='{"code":"refresh_token_reused"}',
                source_stream="json_event.error",
                status_code=401,
                provider_code="refresh_token_reused",
            ),
        ),
    )
    err.caller = "Implementer"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result == AgentCredentialFailureRouteResult(
        status_code=401,
        status_message="operator-actionable agent credential failure: status 401",
        issue_url="https://github.com/owner/consuming-project/issues/42",
    )
    _, _, body, _ = github_svc.create_issue_in.call_args[0]
    assert "Run `codex login` on the host to reseed credentials." in body
    assert '{"code":"refresh_token_reused"}' in body


def test_route_agent_credential_failure_interprets_claude_subscription_access_denial_in_routing_module():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42
    message = (
        "Your organization has disabled Claude subscription access for Claude Code. "
        "Please ask your admin to enable Claude subscription access for Claude Code."
    )
    err = HardAgentError(
        message=message,
        status_code=403,
        service_name="claude",
        observations=(
            ProviderErrorObservation(
                service_name="claude",
                raw_provider_text=message,
                source_stream="result",
                status_code=403,
            ),
        ),
    )
    err.caller = "Planner"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result == AgentCredentialFailureRouteResult(
        status_code=403,
        status_message="operator-actionable agent credential failure: status 403",
        issue_url="https://github.com/owner/consuming-project/issues/42",
    )
    _, _, body, _ = github_svc.create_issue_in.call_args[0]
    assert (
        "Restore Claude Code subscription access or switch to a token with access."
        in body
    )
    assert message in body


def test_route_agent_credential_failure_returns_local_remediation_when_issue_filing_fails():
    github_svc = MagicMock(spec=GithubService)
    github_svc.search_open_issues_by_title.side_effect = RuntimeError("tracker down")

    err = AgentCredentialFailureError(
        message="Codex authentication missing: run `codex login` on the host.",
        status_code=401,
        service_name="codex",
        observations=(
            ProviderErrorObservation(
                service_name="codex",
                raw_provider_text=(
                    "Codex authentication missing: run `codex login` on the host."
                ),
                source_stream="pre-dispatch host check",
                status_code=401,
            ),
        ),
    )
    err.caller = "Failure Report Agent"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result == AgentCredentialFailureRouteResult(
        status_code=401,
        status_message=(
            "operator-actionable agent credential failure: "
            "Run `codex login` on the host to seed Codex credentials before "
            "dispatch. Evidence: Codex authentication missing: run `codex login` "
            "on the host."
        ),
        issue_url=None,
    )


def test_route_agent_credential_failure_uses_raw_error_as_local_evidence_without_observations():
    github_svc = MagicMock(spec=GithubService)
    github_svc.search_open_issues_by_title.side_effect = RuntimeError("tracker down")
    message = "OpenCode request failed: 401 invalid API key for provider opencode-go"
    err = AgentCredentialFailureError(
        message=message,
        status_code=401,
        service_name="opencode",
        classification="operator_actionable_agent_credential_failure",
        observations=(),
    )
    err.caller = "Implementer"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result == AgentCredentialFailureRouteResult(
        status_code=401,
        status_message=(
            "operator-actionable agent credential failure: "
            "Update the configured OpenCode API key and rerun pycastle. "
            "Evidence: OpenCode request failed: 401 invalid API key for provider "
            "opencode-go"
        ),
        issue_url=None,
    )


def test_route_agent_credential_failure_returns_none_for_unrelated_hard_error():
    github_svc = MagicMock(spec=GithubService)
    err = HardAgentError(
        message='{"type":"result","is_error":true,"result":"Unauthorized: invalid token"}',
        status_code=401,
        service_name="codex",
    )
    err.caller = "Implementer"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result is None
    github_svc.search_open_issues_by_title.assert_not_called()
