from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pycastle.errors import (
    ClaudeCliNotFoundError,
    ClaudeCommandError,
    ClaudeServiceError,
    ClaudeTimeoutError,
    PycastleError,
)


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_claude_service_error_is_pycastle_error():
    assert issubclass(ClaudeServiceError, PycastleError)


def test_claude_cli_not_found_error_is_claude_service_error():
    assert issubclass(ClaudeCliNotFoundError, ClaudeServiceError)


def test_claude_timeout_error_is_claude_service_error_and_timeout_error():
    assert issubclass(ClaudeTimeoutError, ClaudeServiceError)
    assert issubclass(ClaudeTimeoutError, TimeoutError)


def test_claude_command_error_is_claude_service_error():
    assert issubclass(ClaudeCommandError, ClaudeServiceError)


# ── helpers ───────────────────────────────────────────────────────────────────


_FAKE_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
]


def _subprocess_ok(models: list[str] = _FAKE_MODELS):
    result = MagicMock()
    result.returncode = 0
    result.stdout = "\n".join(models) + "\n"
    result.stderr = ""
    return result


def _subprocess_fail(returncode: int = 1, stderr: str = "unknown subcommand"):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = ""
    result.stderr = stderr
    return result


@pytest.fixture(autouse=True)
def _clear_list_models_cache():
    from pycastle.claude_service import _list_models

    _list_models.cache_clear()
    yield
    _list_models.cache_clear()


# ── ClaudeService.list_models: success ───────────────────────────────────────


def test_list_models_returns_tuple_of_model_ids():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", return_value=_subprocess_ok()):
        service = ClaudeService()
        models = service.list_models()
    assert isinstance(models, tuple)
    assert "claude-sonnet-4-6" in models


def test_list_models_strips_whitespace_from_lines():
    from pycastle.claude_service import ClaudeService

    result = MagicMock()
    result.returncode = 0
    result.stdout = "  claude-sonnet-4-6  \n  claude-haiku-4-5-20251001  \n"
    result.stderr = ""
    with patch("subprocess.run", return_value=result):
        service = ClaudeService()
        models = service.list_models()
    assert "claude-sonnet-4-6" in models
    assert "claude-haiku-4-5-20251001" in models


def test_list_models_skips_blank_lines():
    from pycastle.claude_service import ClaudeService

    result = MagicMock()
    result.returncode = 0
    result.stdout = "claude-sonnet-4-6\n\n\nclaude-opus-4-7\n"
    result.stderr = ""
    with patch("subprocess.run", return_value=result):
        service = ClaudeService()
        models = service.list_models()
    assert "" not in models
    assert len(models) == 2


# ── ClaudeService.list_models: error paths ───────────────────────────────────


def test_list_models_raises_claude_cli_not_found_error_when_cli_missing():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", side_effect=FileNotFoundError):
        service = ClaudeService()
        with pytest.raises(ClaudeCliNotFoundError):
            service.list_models()


def test_cli_not_found_error_message_mentions_claude():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", side_effect=FileNotFoundError):
        service = ClaudeService()
        with pytest.raises(ClaudeCliNotFoundError) as exc_info:
            service.list_models()
    assert "claude" in str(exc_info.value).lower()


def test_list_models_raises_claude_timeout_error_on_timeout():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)):
        service = ClaudeService()
        with pytest.raises(ClaudeTimeoutError):
            service.list_models()


def test_list_models_raises_claude_command_error_on_nonzero_exit():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", return_value=_subprocess_fail(returncode=1)):
        service = ClaudeService()
        with pytest.raises(ClaudeCommandError):
            service.list_models()


def test_command_error_message_includes_exit_code():
    from pycastle.claude_service import ClaudeService

    with patch(
        "subprocess.run",
        return_value=_subprocess_fail(returncode=127, stderr="not found"),
    ):
        service = ClaudeService()
        with pytest.raises(ClaudeCommandError) as exc_info:
            service.list_models()
    assert "127" in str(exc_info.value)


def test_list_models_raises_claude_service_error_on_empty_response():
    from pycastle.claude_service import ClaudeService

    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    with patch("subprocess.run", return_value=result):
        service = ClaudeService()
        with pytest.raises(ClaudeServiceError):
            service.list_models()


def test_list_models_raises_claude_service_error_on_whitespace_only_response():
    from pycastle.claude_service import ClaudeService

    result = MagicMock()
    result.returncode = 0
    result.stdout = "   \n\n   \n"
    result.stderr = ""
    with patch("subprocess.run", return_value=result):
        service = ClaudeService()
        with pytest.raises(ClaudeServiceError):
            service.list_models()


# ── ClaudeService.list_models: caching ───────────────────────────────────────


def test_list_models_called_once_across_multiple_calls():
    from pycastle.claude_service import ClaudeService

    service = ClaudeService()
    with patch("subprocess.run", return_value=_subprocess_ok()) as mock_run:
        service.list_models()
        service.list_models()
        service.list_models()
    mock_run.assert_called_once()


def test_list_models_cache_is_shared_across_instances():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", return_value=_subprocess_ok()) as mock_run:
        ClaudeService().list_models()
        ClaudeService().list_models()
    mock_run.assert_called_once()


# ── No subprocess exceptions leak to caller ───────────────────────────────────


def test_file_not_found_does_not_propagate_as_original_exception():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", side_effect=FileNotFoundError):
        service = ClaudeService()
        with pytest.raises(ClaudeServiceError):
            service.list_models()
        # FileNotFoundError must NOT propagate
        try:
            service.list_models()
        except FileNotFoundError:
            pytest.fail("FileNotFoundError leaked to caller")
        except ClaudeServiceError:
            pass


def test_timeout_expired_does_not_propagate_as_original_exception():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)):
        service = ClaudeService()
        with pytest.raises(ClaudeServiceError):
            service.list_models()
        try:
            service.list_models()
        except subprocess.TimeoutExpired:
            pytest.fail("TimeoutExpired leaked to caller")
        except ClaudeServiceError:
            pass


def test_permission_error_does_not_propagate_as_original_exception():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", side_effect=PermissionError("permission denied")):
        service = ClaudeService()
        with pytest.raises(ClaudeServiceError):
            service.list_models()


def test_os_error_message_is_preserved():
    from pycastle.claude_service import ClaudeService

    with patch("subprocess.run", side_effect=OSError("some OS error")):
        service = ClaudeService()
        with pytest.raises(ClaudeServiceError) as exc_info:
            service.list_models()
    assert "some OS error" in str(exc_info.value)
