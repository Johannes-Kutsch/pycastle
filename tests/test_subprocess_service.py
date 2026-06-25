import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from pycastle.config import Config
from pycastle.services.git_service import (
    GitCommandError,
    GitNotFoundError,
    GitService,
    GitTimeoutError,
)


def _git_service(timeout: int = 5) -> GitService:
    return GitService(Config(worktree_timeout=timeout))


# ── __init__ ───────────────────────────────────────────────────────────────────


def test_init_assigns_timeout():
    svc = _git_service(timeout=5)
    assert svc.timeout == 5.0


# ── run ────────────────────────────────────────────────────────────────────────


def test_run_returns_true_when_command_succeeds():
    svc = _git_service(timeout=5)
    completed = subprocess.CompletedProcess(
        args=["git", "merge-base", "--is-ancestor", "HEAD", "HEAD"],
        returncode=0,
        stdout=b"",
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed):
        assert svc.is_ancestor("HEAD", Path("repo"))


def test_run_returns_false_when_command_reports_branch_is_not_ancestor():
    svc = _git_service(timeout=5)
    completed = subprocess.CompletedProcess(
        args=["git", "merge-base", "--is-ancestor", "feature", "HEAD"],
        returncode=1,
        stdout=b"",
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed):
        assert not svc.is_ancestor("feature", Path("repo"))


def test_run_raises_timeout_error_when_command_exceeds_timeout():
    svc = _git_service(timeout=5)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git", "merge-base"], timeout=5.0),
    ):
        with pytest.raises(GitTimeoutError):
            svc.is_ancestor("HEAD", Path("repo"))


def test_run_timeout_error_message_includes_cmd_and_duration():
    svc = _git_service(timeout=5)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git", "merge-base"], timeout=5.0),
    ):
        with pytest.raises(GitTimeoutError, match="5.0s"):
            svc.is_ancestor("HEAD", Path("repo"))


def test_run_raises_not_found_error_when_executable_is_missing():
    svc = _git_service(timeout=5)
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(GitNotFoundError):
            svc.is_ancestor("HEAD", Path("repo"))


def test_run_not_found_error_message_includes_executable_name():
    svc = _git_service(timeout=5)
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(GitNotFoundError, match="executable not found: git"):
            svc.is_ancestor("HEAD", Path("repo"))


# ── run_or_raise ───────────────────────────────────────────────────────────────


def test_run_or_raise_propagates_timeout_error():
    svc = _git_service(timeout=5)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["cmd"], timeout=5.0),
    ):
        with pytest.raises(GitTimeoutError):
            svc.get_current_branch(Path("repo"))


def test_run_or_raise_propagates_not_found_error():
    svc = _git_service(timeout=5)
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(GitNotFoundError):
            svc.get_current_branch(Path("repo"))


def test_run_or_raise_returns_completed_process_on_success():
    svc = _git_service(timeout=5)
    completed = subprocess.CompletedProcess(
        args=["git", "rev-parse", "--abbrev-ref", "HEAD"],
        returncode=0,
        stdout=b"main\n",
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed):
        assert svc.get_current_branch(Path("repo")) == "main"


def test_run_or_raise_raises_command_error_on_nonzero_returncode_message_and_fields():
    svc = _git_service(timeout=5)
    failed = subprocess.CompletedProcess(
        args=["git", "rev-parse", "--abbrev-ref", "HEAD"],
        returncode=1,
        stdout=b"",
        stderr=b"  some error  ",
    )
    with patch("subprocess.run", return_value=failed):
        with pytest.raises(GitCommandError) as exc_info:
            svc.get_current_branch(Path("repo"))
    err = exc_info.value
    assert str(err).startswith(
        "git rev-parse --abbrev-ref HEAD failed\nreturncode: 1\nstderr: some error"
    )
    assert err.returncode == 1
    assert err.stderr == "some error"


def test_decode_strips_and_decodes_utf8_bytes():
    svc = _git_service(timeout=5)
    completed = subprocess.CompletedProcess(
        args=["git", "remote", "get-url", "origin"],
        returncode=0,
        stdout=b"  hello world  \n",
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed):
        assert svc.get_remote_url() == "hello world"


def test_decode_empty_bytes_returns_empty_string():
    svc = _git_service(timeout=5)
    completed = subprocess.CompletedProcess(
        args=["git", "remote", "get-url", "origin"],
        returncode=0,
        stdout=b"",
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed):
        assert svc.get_remote_url() == ""


def test_decode_whitespace_only_returns_empty_string():
    svc = _git_service(timeout=5)
    completed = subprocess.CompletedProcess(
        args=["git", "remote", "get-url", "origin"],
        returncode=0,
        stdout=b"   \n\t  ",
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed):
        assert svc.get_remote_url() == ""


def test_decode_handles_invalid_utf8_with_replacement():
    svc = _git_service(timeout=5)
    completed = subprocess.CompletedProcess(
        args=["git", "remote", "get-url", "origin"],
        returncode=0,
        stdout=b"\xff\xfe",
        stderr=b"",
    )
    with patch("subprocess.run", return_value=completed):
        result = svc.get_remote_url()
    assert "�" in result
