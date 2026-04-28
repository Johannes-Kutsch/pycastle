from __future__ import annotations

from unittest.mock import patch

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


@pytest.fixture(autouse=True)
def _clear_list_models_cache():
    from pycastle.claude_service import _list_models

    _list_models.cache_clear()
    yield
    _list_models.cache_clear()


def _with_claude(found: bool = True):
    return patch("shutil.which", return_value="/usr/bin/claude" if found else None)


# ── ClaudeService.list_models: success ───────────────────────────────────────


def test_list_models_returns_tuple_of_model_ids():
    from pycastle.claude_service import ClaudeService

    with _with_claude():
        models = ClaudeService().list_models()
    assert isinstance(models, tuple)
    assert "claude-sonnet-4-6" in models


def test_list_models_returns_known_haiku_model():
    from pycastle.claude_service import ClaudeService

    with _with_claude():
        models = ClaudeService().list_models()
    assert "claude-haiku-4-5-20251001" in models


def test_list_models_returns_known_opus_model():
    from pycastle.claude_service import ClaudeService

    with _with_claude():
        models = ClaudeService().list_models()
    assert "claude-opus-4-7" in models


def test_list_models_contains_no_blank_entries():
    from pycastle.claude_service import ClaudeService

    with _with_claude():
        models = ClaudeService().list_models()
    assert all(m.strip() for m in models)


# ── ClaudeService.list_models: error paths ───────────────────────────────────


def test_list_models_raises_claude_cli_not_found_error_when_cli_missing():
    from pycastle.claude_service import ClaudeService

    with _with_claude(found=False):
        with pytest.raises(ClaudeCliNotFoundError):
            ClaudeService().list_models()


def test_cli_not_found_error_message_mentions_claude():
    from pycastle.claude_service import ClaudeService

    with _with_claude(found=False):
        with pytest.raises(ClaudeCliNotFoundError) as exc_info:
            ClaudeService().list_models()
    assert "claude" in str(exc_info.value).lower()


def test_cli_not_found_error_is_claude_service_error():
    from pycastle.claude_service import ClaudeService

    with _with_claude(found=False):
        with pytest.raises(ClaudeServiceError):
            ClaudeService().list_models()


# ── ClaudeService.list_models: caching ───────────────────────────────────────


def test_list_models_called_once_across_multiple_calls():
    from pycastle.claude_service import ClaudeService

    service = ClaudeService()
    with _with_claude() as mock_which:
        service.list_models()
        service.list_models()
        service.list_models()
    mock_which.assert_called_once()


def test_list_models_cache_is_shared_across_instances():
    from pycastle.claude_service import ClaudeService

    with _with_claude() as mock_which:
        ClaudeService().list_models()
        ClaudeService().list_models()
    mock_which.assert_called_once()
