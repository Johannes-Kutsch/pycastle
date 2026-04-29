import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
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


# ── Config injection ───────────────────────────────────────────────────────────


def test_github_service_uses_worktree_timeout_from_injected_config():
    svc = GithubService("owner/repo", cfg=Config(worktree_timeout=1))
    assert svc.timeout == 1


def test_github_service_default_constructor_uses_config_singleton_timeout():
    svc = GithubService("owner/repo")
    assert svc.timeout == 30


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


def test_close_issue_raises_github_not_found_error_when_gh_missing():
    svc = GithubService("owner/repo")
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(GithubNotFoundError):
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


def test_get_parent_raises_github_command_error_on_non_numeric_output():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"unexpected\n", stderr=b""),
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


def test_get_open_sub_issues_raises_github_command_error_on_invalid_json():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"not valid json", stderr=b""),
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


def _run_ok(stdout: bytes = b"") -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr=b"")


def test_close_issue_with_parents_stops_when_no_parent():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(),  # close_issue(10)
            _run_ok(b"null\n"),  # get_parent(10) -> None
        ],
    ) as mock_run:
        svc.close_issue_with_parents(10)
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == [["gh", "issue", "close", "10"]]


def test_close_issue_with_parents_closes_parent_when_all_siblings_done():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(),  # close_issue(10)
            _run_ok(b"5\n"),  # get_parent(10) -> 5
            _run_ok(b"[]"),  # get_open_sub_issues(5) -> []
            _run_ok(),  # close_issue(5)
            _run_ok(b"null\n"),  # get_parent(5) -> None
        ],
    ) as mock_run:
        svc.close_issue_with_parents(10)
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == [["gh", "issue", "close", "10"], ["gh", "issue", "close", "5"]]


def test_close_issue_with_parents_skips_parent_when_siblings_still_open():
    svc = GithubService("owner/repo")
    siblings = json.dumps([{"number": 11, "state": "open"}]).encode()
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(),  # close_issue(10)
            _run_ok(b"5\n"),  # get_parent(10) -> 5
            _run_ok(siblings),  # get_open_sub_issues(5) -> [11]
        ],
    ) as mock_run:
        svc.close_issue_with_parents(10)
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == [["gh", "issue", "close", "10"]]


# ── has_open_issues_with_label() ──────────────────────────────────────────────


def test_has_open_issues_with_label_returns_true_when_count_greater_than_zero():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"3\n", stderr=b""),
    ):
        assert svc.has_open_issues_with_label("ready-for-agent") is True


def test_has_open_issues_with_label_returns_false_when_count_is_zero():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"0\n", stderr=b""),
    ):
        assert svc.has_open_issues_with_label("ready-for-agent") is False


def test_has_open_issues_with_label_passes_correct_command_args():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"0\n", stderr=b""),
    ) as m:
        svc.has_open_issues_with_label("my-label")
    cmd = m.call_args[0][0]
    assert "--repo" in cmd
    assert "owner/repo" in cmd
    assert "--label" in cmd
    assert "my-label" in cmd
    assert "--state" in cmd
    assert "open" in cmd


def test_has_open_issues_with_label_raises_github_command_error_on_nonzero_exit():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GithubCommandError):
            svc.has_open_issues_with_label("ready-for-agent")


def test_has_open_issues_with_label_raises_github_command_error_on_non_numeric_output():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"not-a-number\n", stderr=b""),
    ):
        with pytest.raises(GithubCommandError):
            svc.has_open_issues_with_label("ready-for-agent")


def test_has_open_issues_with_label_raises_github_timeout_error_on_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc.has_open_issues_with_label("ready-for-agent")


# ── get_labels() ──────────────────────────────────────────────────────────────


def test_get_labels_returns_label_names():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(
            returncode=0, stdout=b"bug\nready-for-agent\n", stderr=b""
        ),
    ):
        result = svc.get_labels(42)
    assert result == ["bug", "ready-for-agent"]


def test_get_labels_returns_empty_list_when_no_labels():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"", stderr=b""),
    ):
        assert svc.get_labels(42) == []


def test_get_labels_raises_github_command_error_on_failure():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GithubCommandError):
            svc.get_labels(42)


def test_get_labels_raises_github_timeout_error_on_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc.get_labels(42)


def test_close_issue_with_parents_closes_chain_recursively():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(),  # close_issue(10)
            _run_ok(b"5\n"),  # get_parent(10) -> 5
            _run_ok(b"[]"),  # get_open_sub_issues(5) -> []
            _run_ok(),  # close_issue(5)
            _run_ok(b"3\n"),  # get_parent(5) -> 3
            _run_ok(b"[]"),  # get_open_sub_issues(3) -> []
            _run_ok(),  # close_issue(3)
            _run_ok(b"null\n"),  # get_parent(3) -> None
        ],
    ) as mock_run:
        svc.close_issue_with_parents(10)
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == [
        ["gh", "issue", "close", "10"],
        ["gh", "issue", "close", "5"],
        ["gh", "issue", "close", "3"],
    ]


# ── get_open_issue_numbers() ─────────────────────────────────────────────────


def test_get_open_issue_numbers_returns_list_of_integers():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"1\n2\n42\n", stderr=b""),
    ):
        assert svc.get_open_issue_numbers() == [1, 2, 42]


def test_get_open_issue_numbers_returns_empty_list_when_no_open_issues():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"", stderr=b""),
    ):
        assert svc.get_open_issue_numbers() == []


def test_get_open_issue_numbers_includes_limit_flag():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"", stderr=b""),
    ) as m:
        svc.get_open_issue_numbers()
    cmd = m.call_args[0][0]
    assert "--limit" in cmd


def test_get_open_issue_numbers_raises_github_command_error_on_failure():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GithubCommandError):
            svc.get_open_issue_numbers()


def test_get_open_issue_numbers_raises_github_command_error_on_non_numeric_output():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"abc\n", stderr=b""),
    ):
        with pytest.raises(GithubCommandError):
            svc.get_open_issue_numbers()


def test_get_open_issue_numbers_raises_github_timeout_error_on_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc.get_open_issue_numbers()


# ── close_completed_parent_issues() ──────────────────────────────────────────


def test_close_completed_parent_issues_closes_parent_when_all_sub_issues_closed():
    svc = GithubService("owner/repo")
    all_subs = json.dumps([{"number": 10, "state": "closed"}]).encode()
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(b"5\n"),  # get_open_issue_numbers() -> [5]
            _run_ok(all_subs),  # get_all_sub_issues(5) -> [{10, closed}]
            _run_ok(),  # close_issue(5)
            _run_ok(b""),  # get_open_issue_numbers() second pass -> []
        ],
    ) as mock_run:
        svc.close_completed_parent_issues()
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == [["gh", "issue", "close", "5"]]


def test_close_completed_parent_issues_does_not_close_issue_with_no_sub_issues():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(b"5\n"),  # get_open_issue_numbers() -> [5]
            _run_ok(b"[]"),  # _get_all_sub_issues(5) -> []
        ],
    ) as mock_run:
        svc.close_completed_parent_issues()
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == []


def test_close_completed_parent_issues_handles_multi_level_chain():
    svc = GithubService("owner/repo")
    parent_subs = json.dumps([{"number": 20, "state": "closed"}]).encode()
    grandparent_subs_first = json.dumps([{"number": 10, "state": "open"}]).encode()
    grandparent_subs_second = json.dumps([{"number": 10, "state": "closed"}]).encode()
    with patch(
        "subprocess.run",
        side_effect=[
            # pass 1: open issues are [10, 100] (10=parent, 100=grandparent)
            _run_ok(b"10\n100\n"),
            _run_ok(parent_subs),  # sub-issues(10) -> [{20,closed}]
            _run_ok(),  # close_issue(10)
            _run_ok(
                grandparent_subs_first
            ),  # sub-issues(100) -> [{10,open}] — stale in pass 1
            # pass 2: re-sweep; open issues are [100]
            _run_ok(b"100\n"),
            _run_ok(grandparent_subs_second),  # sub-issues(100) -> [{10,closed}]
            _run_ok(),  # close_issue(100)
            # pass 3: no open issues
            _run_ok(b""),
        ],
    ) as mock_run:
        svc.close_completed_parent_issues()
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == [["gh", "issue", "close", "10"], ["gh", "issue", "close", "100"]]


def test_close_completed_parent_issues_does_not_close_issue_with_open_sub_issues():
    svc = GithubService("owner/repo")
    mixed = json.dumps(
        [
            {"number": 10, "state": "closed"},
            {"number": 11, "state": "open"},
        ]
    ).encode()
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(b"5\n"),  # get_open_issue_numbers() -> [5]
            _run_ok(mixed),  # _get_all_sub_issues(5) -> [{10,closed},{11,open}]
        ],
    ) as mock_run:
        svc.close_completed_parent_issues()
    closed = [
        c[0][0]
        for c in mock_run.call_args_list
        if c[0][0][:3] == ["gh", "issue", "close"]
    ]
    assert closed == []


def test_close_completed_parent_issues_propagates_error_from_sub_issues_api():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=[
            _run_ok(b"5\n"),  # get_open_issue_numbers() -> [5]
            MagicMock(
                returncode=1, stdout=b"", stderr=b"api error"
            ),  # _get_all_sub_issues(5) fails
        ],
    ):
        with pytest.raises(GithubCommandError):
            svc.close_completed_parent_issues()


# ── get_open_issues() ─────────────────────────────────────────────────────────


def test_get_open_issues_returns_correctly_shaped_list():
    svc = GithubService("owner/repo")
    payload = json.dumps(
        [
            {
                "number": 42,
                "title": "Fix bug",
                "body": "Some body",
                "labels": ["bug", "ready-for-agent"],
                "comments": ["First comment", "Second comment"],
            }
        ]
    ).encode()
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=payload, stderr=b""),
    ):
        result = svc.get_open_issues("ready-for-agent")
    assert len(result) == 1
    issue = result[0]
    assert issue["number"] == 42
    assert issue["title"] == "Fix bug"
    assert issue["body"] == "Some body"
    assert issue["labels"] == ["bug", "ready-for-agent"]
    assert issue["comments"] == ["First comment", "Second comment"]


def test_get_open_issues_passes_correct_command_args():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"[]", stderr=b""),
    ) as m:
        svc.get_open_issues("my-label")
    cmd = m.call_args[0][0]
    assert "--repo" in cmd
    assert "owner/repo" in cmd
    assert "--state" in cmd
    assert "open" in cmd
    assert "--label" in cmd
    assert "my-label" in cmd
    assert "--json" in cmd
    assert "--jq" in cmd
    jq_expr = cmd[cmd.index("--jq") + 1]
    assert "labels[].name" in jq_expr
    assert "comments[].body" in jq_expr


def test_get_open_issues_returns_empty_list_when_no_issues():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"[]", stderr=b""),
    ):
        assert svc.get_open_issues("ready-for-agent") == []


def test_get_open_issues_raises_github_command_error_on_nonzero_exit():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=1, stdout=b"", stderr=b"error"),
    ):
        with pytest.raises(GithubCommandError):
            svc.get_open_issues("ready-for-agent")


def test_get_open_issues_raises_github_command_error_on_invalid_json():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        return_value=MagicMock(returncode=0, stdout=b"not valid json", stderr=b""),
    ):
        with pytest.raises(GithubCommandError):
            svc.get_open_issues("ready-for-agent")


def test_get_open_issues_raises_github_timeout_error_on_timeout():
    svc = GithubService("owner/repo")
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30),
    ):
        with pytest.raises(GithubTimeoutError):
            svc.get_open_issues("ready-for-agent")
