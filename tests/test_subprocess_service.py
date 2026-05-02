import subprocess
from unittest.mock import patch

import pytest

from pycastle.services._base import _SubprocessService


class _ConcreteTimeoutError(RuntimeError):
    pass


class _ConcreteNotFoundError(RuntimeError):
    pass


class _ConcreteCommandError(RuntimeError):
    def __init__(self, message: str, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)


class _ConcreteService(_SubprocessService):
    _timeout_error_class = _ConcreteTimeoutError
    _not_found_error_class = _ConcreteNotFoundError
    _command_error_class = _ConcreteCommandError


# ── __init__ ───────────────────────────────────────────────────────────────────


def test_init_assigns_timeout():
    svc = _ConcreteService(timeout=5.0)
    assert svc.timeout == 5.0


# ── _run ───────────────────────────────────────────────────────────────────────


def test_run_returns_completed_process_on_success():
    svc = _ConcreteService(timeout=5.0)
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout=b"", stderr=b""
    )
    with patch("subprocess.run", return_value=completed):
        result = svc._run(["echo"])
    assert result is completed


def test_run_translates_timeout_expired_to_timeout_error_class():
    svc = _ConcreteService(timeout=5.0)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["echo"], timeout=5.0),
    ):
        with pytest.raises(_ConcreteTimeoutError):
            svc._run(["echo"])


def test_run_translates_file_not_found_to_not_found_error_class():
    svc = _ConcreteService(timeout=5.0)
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(_ConcreteNotFoundError):
            svc._run(["no-such-exe"])


# ── _run_or_raise ──────────────────────────────────────────────────────────────


def test_run_or_raise_returns_completed_process_on_success():
    svc = _ConcreteService(timeout=5.0)
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout=b"ok", stderr=b""
    )
    with patch("subprocess.run", return_value=completed):
        result = svc._run_or_raise(["echo"], message="failed")
    assert result is completed


def test_run_or_raise_raises_command_error_on_nonzero_returncode():
    svc = _ConcreteService(timeout=5.0)
    failed = subprocess.CompletedProcess(
        args=["cmd"], returncode=1, stdout=b"", stderr=b"  some error  "
    )
    with patch("subprocess.run", return_value=failed):
        with pytest.raises(_ConcreteCommandError) as exc_info:
            svc._run_or_raise(["cmd"], message="cmd failed")
    err = exc_info.value
    assert str(err) == "cmd failed"
    assert err.returncode == 1
    assert err.stderr == "some error"


# ── _decode ────────────────────────────────────────────────────────────────────


def test_decode_strips_and_decodes_utf8_bytes():
    assert _ConcreteService._decode(b"  hello world  \n") == "hello world"


def test_decode_handles_invalid_utf8_with_replacement():
    result = _ConcreteService._decode(b"\xff\xfe")
    assert isinstance(result, str)
