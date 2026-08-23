"""Tests for upstream_issue_filing — deduped issue filing module."""

from __future__ import annotations

import pytest


def _make_github_svc():
    from unittest.mock import MagicMock

    from pycastle.services import GithubService

    svc = MagicMock(spec=GithubService)
    svc.repo = "consumer/owner"
    svc.search_open_issues_by_title.return_value = []
    svc.create_issue_in.return_value = (123, 10123)
    return svc


# ── Dedupe: existing open issue is returned ───────────────────────────────────


def test_returns_existing_issue_number_when_search_matches():
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.return_value = [99]

    result = file_deduped_upstream_issue(
        dedupe_query="[pycastle] some prefix",
        title="[pycastle] some prefix: detail",
        body="body text",
        labels=["bug"],
        github_svc=svc,
    )

    assert result == 99
    svc.create_issue_in.assert_not_called()


def test_returns_first_match_when_multiple_existing_issues():
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.return_value = [77, 88]

    result = file_deduped_upstream_issue(
        dedupe_query="[pycastle] some prefix",
        title="title",
        body="body",
        labels=["bug"],
        github_svc=svc,
    )

    assert result == 77
    svc.create_issue_in.assert_not_called()


# ── Create: no existing issue triggers create ─────────────────────────────────


def test_creates_issue_when_no_existing_match():
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()

    result = file_deduped_upstream_issue(
        dedupe_query="[pycastle] some prefix",
        title="the title",
        body="the body",
        labels=["bug", "needs-triage"],
        github_svc=svc,
    )

    assert result == 123
    svc.create_issue_in.assert_called_once_with(
        "consumer/owner", "the title", "the body", ["bug", "needs-triage"]
    )


def test_create_uses_github_svc_repo():
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.repo = "other-org/other-repo"
    svc.create_issue_in.return_value = (55, 10055)

    result = file_deduped_upstream_issue(
        dedupe_query="prefix",
        title="t",
        body="b",
        labels=[],
        github_svc=svc,
    )

    assert result == 55
    call_repo = svc.create_issue_in.call_args.args[0]
    assert call_repo == "other-org/other-repo"


# ── Echo: successful create emits the filed line ─────────────────────────────


def test_successful_create_emits_filed_line(capsys):
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()

    file_deduped_upstream_issue(
        dedupe_query="prefix",
        title="My Issue Title",
        body="body",
        labels=["bug"],
        github_svc=svc,
    )

    out = capsys.readouterr().out
    assert "Filed issue #123 on consumer/owner: My Issue Title" in out


def test_no_echo_when_existing_issue_returned(capsys):
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.return_value = [99]

    file_deduped_upstream_issue(
        dedupe_query="prefix",
        title="title",
        body="body",
        labels=["bug"],
        github_svc=svc,
    )

    out = capsys.readouterr().out
    assert "Filed issue" not in out


# ── Search error: GithubServiceError treated as no match ─────────────────────


def test_search_github_service_error_proceeds_to_create():
    from pycastle.services import GithubNetworkError
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.side_effect = GithubNetworkError(
        "dns fail", cause=OSError("dns")
    )

    result = file_deduped_upstream_issue(
        dedupe_query="prefix",
        title="title",
        body="body",
        labels=["bug"],
        github_svc=svc,
    )

    assert result == 123
    svc.create_issue_in.assert_called_once()


def test_search_github_api_error_proceeds_to_create():
    from pycastle.services import GithubAPIError
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.side_effect = GithubAPIError(
        "bad", status=503, body="down", method="GET", path="/search"
    )

    result = file_deduped_upstream_issue(
        dedupe_query="prefix",
        title="title",
        body="body",
        labels=["bug"],
        github_svc=svc,
    )

    assert result == 123
    svc.create_issue_in.assert_called_once()


# ── Create error: GithubServiceError returns None ────────────────────────────


def test_create_github_service_error_returns_none():
    from pycastle.services import GithubNetworkError
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.create_issue_in.side_effect = GithubNetworkError(
        "create failed", cause=OSError("refused")
    )

    result = file_deduped_upstream_issue(
        dedupe_query="prefix",
        title="title",
        body="body",
        labels=["bug"],
        github_svc=svc,
    )

    assert result is None


def test_create_github_api_error_returns_none():
    from pycastle.services import GithubAPIError
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.create_issue_in.side_effect = GithubAPIError(
        "500", status=500, body="err", method="POST", path="/repos/x/issues"
    )

    result = file_deduped_upstream_issue(
        dedupe_query="prefix",
        title="title",
        body="body",
        labels=["bug"],
        github_svc=svc,
    )

    assert result is None


# ── Non-GithubServiceError propagates unchanged ───────────────────────────────


def test_search_non_github_error_propagates():
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.search_open_issues_by_title.side_effect = RuntimeError("unexpected")

    with pytest.raises(RuntimeError, match="unexpected"):
        file_deduped_upstream_issue(
            dedupe_query="prefix",
            title="title",
            body="body",
            labels=["bug"],
            github_svc=svc,
        )


def test_create_non_github_error_propagates():
    from pycastle.upstream_issue_filing import file_deduped_upstream_issue

    svc = _make_github_svc()
    svc.create_issue_in.side_effect = ValueError("unexpected shape")

    with pytest.raises(ValueError, match="unexpected shape"):
        file_deduped_upstream_issue(
            dedupe_query="prefix",
            title="title",
            body="body",
            labels=["bug"],
            github_svc=svc,
        )
