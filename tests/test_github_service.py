import json
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from pycastle.github_service import (
    GithubCommandError,
    GithubNotFoundError,
    GithubService,
    GithubServiceError,
    GithubTimeoutError,
)


# ── Exception hierarchy ────────────────────────────────────────────────────────


def test_github_service_error_is_runtime_error():
    assert issubclass(GithubServiceError, RuntimeError)


def test_github_command_error_is_github_service_error():
    assert issubclass(GithubCommandError, GithubServiceError)


def test_github_timeout_error_is_github_service_error_and_timeout_error():
    assert issubclass(GithubTimeoutError, GithubServiceError)
    assert issubclass(GithubTimeoutError, TimeoutError)


def test_github_not_found_error_is_github_service_error():
    assert issubclass(GithubNotFoundError, GithubServiceError)


def test_github_command_error_carries_returncode_and_stderr():
    err = GithubCommandError("msg", returncode=1, stderr="not found")
    assert err.returncode == 1
    assert err.stderr == "not found"


# ── _run() wrapper ─────────────────────────────────────────────────────────────


def test_run_raises_github_timeout_error_on_subprocess_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc._run(["gh", "issue", "close", "1"])


def test_run_raises_github_not_found_error_when_gh_missing():
    svc = GithubService("owner/repo")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(GithubNotFoundError):
            svc._run(["gh", "issue", "close", "1"])


def test_run_returns_completed_process_on_success():
    svc = GithubService("owner/repo")
    mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
    with patch("subprocess.run", return_value=mock_result):
        result = svc._run(["gh", "issue", "close", "1"], capture_output=True)
    assert result.returncode == 0


def test_run_applies_default_timeout():
    svc = GithubService("owner/repo", timeout=42)
    mock_result = MagicMock(returncode=0, stdout=b"", stderr=b"")
    with patch("subprocess.run", return_value=mock_result) as m:
        svc._run(["gh", "issue", "close", "1"])
    assert m.call_args.kwargs.get("timeout") == 42


# ── close_issue() ──────────────────────────────────────────────────────────────


def test_close_issue_calls_gh_issue_close():
    svc = GithubService("owner/repo")
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")) as m:
        svc.close_issue(42)
    cmd = m.call_args[0][0]
    assert cmd == ["gh", "issue", "close", "42"]


def test_close_issue_raises_github_command_error_on_failure():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stderr=b"not found"),
    ):
        with pytest.raises(GithubCommandError):
            svc.close_issue(99)


def test_close_issue_raises_github_timeout_error_on_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc.close_issue(1)


# ── get_parent() ───────────────────────────────────────────────────────────────


def test_get_parent_returns_parent_number():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"72\n", stderr=b""),
    ):
        assert svc.get_parent(98) == 72


def test_get_parent_returns_none_when_no_parent():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"null\n", stderr=b""),
    ):
        assert svc.get_parent(98) is None


def test_get_parent_returns_none_when_output_empty():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"", stderr=b""),
    ):
        assert svc.get_parent(98) is None


def test_get_parent_calls_correct_api_endpoint():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"null\n", stderr=b""),
    ) as m:
        svc.get_parent(5)
    cmd = m.call_args[0][0]
    assert "repos/owner/repo/issues/5" in " ".join(cmd)
    assert ".parent.number" in cmd


def test_get_parent_raises_github_command_error_on_failure():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GithubCommandError):
            svc.get_parent(5)


def test_get_parent_raises_github_timeout_error_on_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc.get_parent(5)


# ── get_open_sub_issues() ──────────────────────────────────────────────────────


def test_get_open_sub_issues_returns_open_issue_numbers():
    svc = GithubService("owner/repo")
    payload = json.dumps(
        [
            {"number": 10, "state": "open"},
            {"number": 11, "state": "closed"},
            {"number": 12, "state": "open"},
        ]
    ).encode()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=payload, stderr=b""),
    ):
        result = svc.get_open_sub_issues(5)
    assert result == [10, 12]


def test_get_open_sub_issues_returns_empty_list_when_none_open():
    svc = GithubService("owner/repo")
    payload = json.dumps([{"number": 10, "state": "closed"}]).encode()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=payload, stderr=b""),
    ):
        assert svc.get_open_sub_issues(5) == []


def test_get_open_sub_issues_returns_empty_list_for_no_sub_issues():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"[]", stderr=b""),
    ):
        assert svc.get_open_sub_issues(5) == []


def test_get_open_sub_issues_calls_correct_api_endpoint():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"[]", stderr=b""),
    ) as m:
        svc.get_open_sub_issues(7)
    cmd = m.call_args[0][0]
    assert "repos/owner/repo/issues/7/sub_issues" in " ".join(cmd)


def test_get_open_sub_issues_raises_github_command_error_on_failure():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GithubCommandError):
            svc.get_open_sub_issues(5)


def test_get_open_sub_issues_raises_github_timeout_error_on_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc.get_open_sub_issues(5)


# ── close_issue_with_parents() ────────────────────────────────────────────────


def test_close_issue_with_parents_closes_issue():
    svc = GithubService("owner/repo")
    with (
        patch.object(svc, "close_issue") as mock_close,
        patch.object(svc, "get_parent", return_value=None),
    ):
        svc.close_issue_with_parents(10)
    mock_close.assert_called_once_with(10)


def test_close_issue_with_parents_stops_when_no_parent():
    svc = GithubService("owner/repo")
    with (
        patch.object(svc, "close_issue") as mock_close,
        patch.object(svc, "get_parent", return_value=None),
    ):
        svc.close_issue_with_parents(10)
    assert mock_close.call_count == 1


def test_close_issue_with_parents_closes_parent_when_all_siblings_done():
    svc = GithubService("owner/repo")
    with (
        patch.object(svc, "close_issue") as mock_close,
        patch.object(svc, "get_parent", side_effect=[5, None]),
        patch.object(svc, "get_open_sub_issues", return_value=[]),
    ):
        svc.close_issue_with_parents(10)
    assert mock_close.call_args_list == [call(10), call(5)]


def test_close_issue_with_parents_skips_parent_when_siblings_still_open():
    svc = GithubService("owner/repo")
    with (
        patch.object(svc, "close_issue") as mock_close,
        patch.object(svc, "get_parent", return_value=5),
        patch.object(svc, "get_open_sub_issues", return_value=[11]),
    ):
        svc.close_issue_with_parents(10)
    mock_close.assert_called_once_with(10)


def test_close_issue_with_parents_closes_chain_recursively():
    svc = GithubService("owner/repo")
    with (
        patch.object(svc, "close_issue") as mock_close,
        patch.object(svc, "get_parent", side_effect=[5, 3, None]),
        patch.object(svc, "get_open_sub_issues", return_value=[]),
    ):
        svc.close_issue_with_parents(10)
    assert mock_close.call_args_list == [call(10), call(5), call(3)]
