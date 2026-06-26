from unittest.mock import MagicMock

import pytest

from pycastle.agent_credential_failure_routing import (
    AgentCredentialFailureRouteResult,
    route_agent_credential_failure,
)
from pycastle.errors import AgentCredentialFailureError, HardAgentError
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


def test_route_agent_credential_failure_builds_redacted_issue_body_in_routing_module():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42
    raw_result = (
        '{"type":"error","code":"refresh_token_reused","apiKey":"plain-secret-123456",'
        '"message":"The access token sk-live-abc123SECRET could not be refreshed."}'
    )
    err = AgentCredentialFailureError(
        message=raw_result,
        status_code=401,
        service_name="codex",
        classification="codex_auth_lineage_exhausted",
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
    _, _, body, labels = github_svc.create_issue_in.call_args[0]
    assert labels == ["bug", "needs-triage"]
    assert "Service: codex" in body
    assert "Agent: Implementer" in body
    assert "Status: 401" in body
    assert "## Environment" in body
    assert "### stderr" in body
    assert "### Raw result envelope" in body
    assert "plain-secret-123456" not in body
    assert "rt-secret-123456" not in body
    assert "sk-live-abc123SECRET" not in body
    assert body.count("[REDACTED]") >= 3


def test_route_agent_credential_failure_files_stable_codex_issue_contract_at_module_seam():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42
    raw_result = (
        '{"type":"error","code":"refresh_token_reused","apiKey":"plain-secret-123456",'
        '"message":"The access token sk-live-abc123SECRET could not be refreshed."}'
    )
    err = AgentCredentialFailureError(
        message=raw_result,
        status_code=401,
        service_name="codex",
        classification="operator_actionable_agent_credential_failure",
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
    github_svc.create_issue_in.assert_called_once()
    owner_repo, title, body, labels = github_svc.create_issue_in.call_args[0]
    assert owner_repo == "owner/consuming-project"
    assert title == "[pycastle] operator-actionable agent credential failure"
    assert labels == ["bug", "needs-triage"]
    assert "Repair local agent credentials/account access and rerun pycastle." in body
    assert (
        "This issue is about local agent-provider credentials/account access, "
        "not a source-code defect in the consuming project."
    ) in body
    assert "Run `codex login` on the host to reseed credentials." in body
    assert "Agent: Implementer" in body
    assert "Service: codex" in body
    assert "Status: 401" in body
    assert "### stderr" in body
    assert "### Raw result envelope" in body
    assert "## Environment" in body
    assert (
        "The access token could not be refreshed because "
        "refreshToken=[REDACTED] was already used."
    ) in body
    assert '"code":"refresh_token_reused"' in body
    assert "plain-secret-123456" not in body
    assert "rt-secret-123456" not in body
    assert "sk-live-abc123SECRET" not in body
    assert body.count("[REDACTED]") >= 3


def test_route_agent_credential_failure_selects_codex_reseed_remediation_from_adapter_classification():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42

    err = AgentCredentialFailureError(
        message="Codex request failed with credential failure.",
        status_code=401,
        service_name="codex",
        classification="codex_auth_lineage_exhausted",
    )
    err.caller = "Implementer"

    route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    _, _, body, _ = github_svc.create_issue_in.call_args[0]
    assert "Run `codex login` on the host to reseed credentials." in body


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
        "Restore Claude Code subscription access or use a token/account with "
        "access and rerun pycastle." in body
    )
    assert message in body


def test_route_agent_credential_failure_uses_shared_claude_subscription_remediation_at_module_seam():
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
    )
    err.caller = "Planner"

    route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    _, _, body, _ = github_svc.create_issue_in.call_args[0]
    assert (
        "Restore Claude Code subscription access or use a token/account with "
        "access and rerun pycastle."
    ) in body


def test_route_agent_credential_failure_selects_claude_access_remediation_from_adapter_classification():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42

    err = AgentCredentialFailureError(
        message="Claude request failed with credential failure.",
        status_code=403,
        service_name="claude",
        classification="operator_actionable_agent_credential_failure",
    )
    err.caller = "Planner"

    route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    _, _, body, _ = github_svc.create_issue_in.call_args[0]
    assert (
        "Restore Claude Code subscription access or use a token/account with "
        "access and rerun pycastle."
    ) in body


def test_route_agent_credential_failure_reuses_existing_family_issue_in_routing_module():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = [77]

    err = AgentCredentialFailureError(
        message="OpenCode request failed: 401 invalid API key for provider opencode-go",
        status_code=401,
        service_name="opencode",
        classification="operator_actionable_agent_credential_failure",
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
            "reusing existing issue #77 "
            "(https://github.com/owner/consuming-project/issues/77)"
        ),
        issue_url="https://github.com/owner/consuming-project/issues/77",
    )
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_not_called()


def test_route_agent_credential_failure_reports_reused_issue_in_terminal_status_facts():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = [77]

    err = AgentCredentialFailureError(
        message="OpenCode request failed: 401 invalid API key for provider opencode-go",
        status_code=401,
        service_name="opencode",
        classification="operator_actionable_agent_credential_failure",
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
            "reusing existing issue #77 "
            "(https://github.com/owner/consuming-project/issues/77)"
        ),
        issue_url="https://github.com/owner/consuming-project/issues/77",
    )
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_not_called()


def test_route_agent_credential_failure_returns_local_remediation_when_issue_filing_fails():
    github_svc = MagicMock(spec=GithubService)
    github_svc.search_open_issues_by_title.side_effect = RuntimeError("tracker down")

    err = AgentCredentialFailureError(
        message="Codex authentication missing: run `codex login` on the host.",
        status_code=401,
        service_name="codex",
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


def test_route_agent_credential_failure_redacts_local_fallback_evidence_when_issue_lookup_fails():
    github_svc = MagicMock(spec=GithubService)
    github_svc.search_open_issues_by_title.side_effect = RuntimeError("tracker down")

    err = AgentCredentialFailureError(
        message=(
            "Codex authentication missing: accessToken=at-secret-123456; "
            "run `codex login` on the host."
        ),
        status_code=401,
        service_name="codex",
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
            "dispatch. Evidence: Codex authentication missing: "
            "accessToken=[REDACTED]; run `codex login` on the host."
        ),
        issue_url=None,
    )
    github_svc.create_issue_in.assert_not_called()


def test_route_agent_credential_failure_returns_local_remediation_when_issue_creation_fails():
    github_svc = MagicMock(spec=GithubService)
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.side_effect = RuntimeError("tracker write failed")

    err = AgentCredentialFailureError(
        message="Codex authentication missing: run `codex login` on the host.",
        status_code=401,
        service_name="codex",
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


def test_route_agent_credential_failure_selects_opencode_api_key_remediation_from_adapter_classification():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 42

    err = AgentCredentialFailureError(
        message="OpenCode request failed with credential failure.",
        status_code=401,
        service_name="opencode",
        classification="operator_actionable_agent_credential_failure",
    )
    err.caller = "Implementer"

    route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    _, _, body, _ = github_svc.create_issue_in.call_args[0]
    assert "Update the configured OpenCode API key and rerun pycastle." in body


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


@pytest.mark.parametrize(
    ("message", "observation_text"),
    [
        (
            "Codex request failed: 401",
            "Codex request failed: 401",
        ),
        (
            "Error: API request failed: 401 Unauthorized",
            "Error: API request failed: 401 Unauthorized",
        ),
        (
            "OAuth exchange failed with invalid_grant",
            "OAuth exchange failed with invalid_grant",
        ),
        (
            "Unauthorized: invalid token",
            "Unauthorized: invalid token",
        ),
        (
            "Request rejected: missing bearer token",
            "Request rejected: missing bearer token",
        ),
        (
            "basic-authentication credentials are missing",
            "basic-authentication credentials are missing",
        ),
    ],
)
def test_route_agent_credential_failure_does_not_route_generic_codex_auth_looking_401s(
    message, observation_text
):
    github_svc = MagicMock(spec=GithubService)
    err = AgentCredentialFailureError(
        message=message,
        status_code=401,
        service_name="codex",
        classification="operator_actionable_agent_credential_failure",
    )
    err.caller = "Implementer"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result is None
    github_svc.search_open_issues_by_title.assert_not_called()
    github_svc.create_issue_in.assert_not_called()


def test_route_agent_credential_failure_does_not_route_generic_codex_auth_looking_error_without_classification():
    github_svc = MagicMock(spec=GithubService)
    err = AgentCredentialFailureError(
        message="Unauthorized: invalid token",
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
    github_svc.create_issue_in.assert_not_called()


def test_route_agent_credential_failure_files_new_issue_when_no_open_family_issue_exists():
    github_svc = MagicMock(spec=GithubService)
    github_svc.repo = "owner/consuming-project"
    github_svc.search_open_issues_by_title.return_value = []
    github_svc.create_issue_in.return_value = 88

    err = AgentCredentialFailureError(
        message="OpenCode request failed: 401 invalid API key for provider opencode-go",
        status_code=401,
        service_name="opencode",
        classification="operator_actionable_agent_credential_failure",
    )
    err.caller = "Implementer"

    result = route_agent_credential_failure(
        provider_failure=err,
        github_svc=github_svc,
    )

    assert result == AgentCredentialFailureRouteResult(
        status_code=401,
        status_message="operator-actionable agent credential failure: status 401",
        issue_url="https://github.com/owner/consuming-project/issues/88",
    )
    github_svc.search_open_issues_by_title.assert_called_once_with(
        "[pycastle] operator-actionable agent credential failure"
    )
    github_svc.create_issue_in.assert_called_once()
