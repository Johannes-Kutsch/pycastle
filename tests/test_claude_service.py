from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pycastle.errors import (
    ClaudeCliNotFoundError,
    ClaudeCommandError,
    ClaudeServiceError,
    ClaudeTimeoutError,
    PycastleError,
)
from pycastle.services import ClaudeService
from pycastle.services.agent_service import AssistantTurn, Result, Tokens, UsageLimit
from pycastle.session_resume import RunKind


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
    from pycastle.services.claude_service import _list_models

    _list_models.cache_clear()
    yield
    _list_models.cache_clear()


def _with_claude(found: bool = True):
    return patch("shutil.which", return_value="/usr/bin/claude" if found else None)


# ── ClaudeService.list_models: success ───────────────────────────────────────


def test_list_models_returns_tuple_of_model_ids():
    with _with_claude():
        models = ClaudeService().list_models()
    assert isinstance(models, tuple)
    assert "claude-sonnet-4-6" in models


def test_list_models_returns_known_haiku_model():
    with _with_claude():
        models = ClaudeService().list_models()
    assert "claude-haiku-4-5-20251001" in models


def test_list_models_returns_known_opus_model():
    with _with_claude():
        models = ClaudeService().list_models()
    assert "claude-opus-4-7" in models


def test_list_models_contains_no_blank_entries():
    with _with_claude():
        models = ClaudeService().list_models()
    assert all(m.strip() for m in models)


# ── ClaudeService.list_models: error paths ───────────────────────────────────


def test_list_models_raises_claude_cli_not_found_error_when_cli_missing():
    with _with_claude(found=False):
        with pytest.raises(ClaudeCliNotFoundError):
            ClaudeService().list_models()


def test_cli_not_found_error_message_mentions_claude():
    with _with_claude(found=False):
        with pytest.raises(ClaudeCliNotFoundError) as exc_info:
            ClaudeService().list_models()
    assert "claude" in str(exc_info.value).lower()


def test_cli_not_found_error_is_claude_service_error():
    with _with_claude(found=False):
        with pytest.raises(ClaudeServiceError):
            ClaudeService().list_models()


# ── ClaudeService.list_models: caching ───────────────────────────────────────


def test_list_models_called_once_across_multiple_calls():
    service = ClaudeService()
    with _with_claude() as mock_which:
        service.list_models()
        service.list_models()
        service.list_models()
    mock_which.assert_called_once()


def test_list_models_cache_is_shared_across_instances():
    with _with_claude() as mock_which:
        ClaudeService().list_models()
        ClaudeService().list_models()
    mock_which.assert_called_once()


# ── ClaudeService.name ────────────────────────────────────────────────────────


def test_claude_service_name_is_claude():
    assert ClaudeService().name == "claude"


# ── ClaudeService.build_command ───────────────────────────────────────────────


def test_build_command_includes_output_format_stream_json():
    assert "--output-format stream-json" in ClaudeService().build_command()


def test_build_command_includes_dangerously_skip_permissions():
    assert "--dangerously-skip-permissions" in ClaudeService().build_command()


def test_build_command_includes_verbose():
    assert "--verbose" in ClaudeService().build_command()


def test_build_command_includes_stdin_redirect():
    assert "< /tmp/.pycastle_prompt" in ClaudeService().build_command()


def test_build_command_includes_model_when_set():
    assert "--model claude-opus-4-7" in ClaudeService().build_command(
        model="claude-opus-4-7"
    )


def test_build_command_includes_effort_when_set():
    assert "--effort high" in ClaudeService().build_command(effort="high")


def test_build_command_excludes_flags_when_unset():
    cmd = ClaudeService().build_command()
    assert "--model" not in cmd
    assert "--effort" not in cmd


def test_build_command_uses_session_id_for_fresh_run_with_uuid():
    cmd = ClaudeService().build_command(run_kind=RunKind.FRESH, session_uuid="abc-123")
    assert "--session-id abc-123" in cmd
    assert "--resume" not in cmd


def test_build_command_uses_resume_flag_for_resume_run_with_uuid():
    cmd = ClaudeService().build_command(run_kind=RunKind.RESUME, session_uuid="abc-123")
    assert "--resume abc-123" in cmd
    assert "--session-id" not in cmd


def test_build_command_omits_session_flags_when_no_uuid():
    cmd = ClaudeService().build_command()
    assert "--session-id" not in cmd
    assert "--resume" not in cmd


# ── ClaudeService.build_env ───────────────────────────────────────────────────


def test_build_env_sets_oauth_token_when_token_provided():
    env = ClaudeService().build_env(token="tok-abc")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-abc"


def test_build_env_sets_config_dir_when_path_provided():
    env = ClaudeService().build_env(
        state_dir_container_path="/home/agent/workspace/.pycastle-session/implementer/"
    )
    assert (
        env["CLAUDE_CONFIG_DIR"]
        == "/home/agent/workspace/.pycastle-session/implementer/"
    )


def test_build_env_sets_both_when_both_provided():
    env = ClaudeService().build_env(
        state_dir_container_path="/home/agent/workspace/.pycastle-session/implementer/",
        token="tok-xyz",
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-xyz"
    assert (
        env["CLAUDE_CONFIG_DIR"]
        == "/home/agent/workspace/.pycastle-session/implementer/"
    )


def test_build_env_returns_empty_dict_when_nothing_provided():
    assert ClaudeService().build_env() == {}


def test_build_env_omits_token_key_when_token_is_none():
    env = ClaudeService().build_env(token=None)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_build_env_omits_config_dir_key_when_path_is_none():
    env = ClaudeService().build_env(state_dir_container_path=None)
    assert "CLAUDE_CONFIG_DIR" not in env


# ── ClaudeService.run ─────────────────────────────────────────────────────────


def _assistant_line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _assistant_with_usage_line(text: str, input_tokens: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}],
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
    )


def _result_line(content: str) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "result": content, "is_error": False}
    )


def _usage_limit_line() -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "rate limit",
        }
    )


def test_run_yields_assistant_turn_for_assistant_lines():
    events = list(ClaudeService().run([_assistant_line("hello")]))
    assert any(isinstance(e, AssistantTurn) and e.text == "hello" for e in events)


def test_run_yields_tokens_when_usage_present():
    events = list(ClaudeService().run([_assistant_with_usage_line("hi", 1000)]))
    assert any(isinstance(e, Tokens) and e.count == 1000 for e in events)


def test_run_yields_result_for_result_line():
    events = list(ClaudeService().run([_result_line("done")]))
    assert any(isinstance(e, Result) and e.text == "done" for e in events)


def test_run_yields_usage_limit_for_429_line():
    events = list(ClaudeService().run([_usage_limit_line()]))
    assert any(isinstance(e, UsageLimit) for e in events)


def test_run_stops_after_result():
    lines = [_result_line("done"), _assistant_line("after")]
    events = list(ClaudeService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_stops_after_usage_limit():
    lines = [_usage_limit_line(), _assistant_line("after")]
    events = list(ClaudeService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_skips_non_json_lines_silently():
    lines = ["not json", _result_line("done")]
    events = list(ClaudeService().run(lines))
    assert any(isinstance(e, Result) for e in events)
