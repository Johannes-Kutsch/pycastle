from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pycastle._time as _time_module
from pycastle.agents.output_protocol import AgentRole
from pycastle.errors import (
    ClaudeCliNotFoundError,
    ClaudeCommandError,
    ClaudeServiceError,
    ClaudeTimeoutError,
    PycastleError,
)
from pycastle.services.agent_service import (
    AssistantTurn as RuntimeAssistantTurn,
    CredentialFailure as RuntimeCredentialFailure,
    HardError as RuntimeHardError,
    PromptTokens as RuntimePromptTokens,
    Result as RuntimeResult,
    TransientError as RuntimeTransientError,
    UsageLimit as RuntimeUsageLimit,
)
from pycastle.runtime_session import ProviderSessionStateRequest
from pycastle.services import ClaudeService
from pycastle.services.agent_service import (
    AssistantTurn,
    PromptTokens,
    Result,
    UsageLimit,
)
from pycastle.session.service_session_store import save_service_session_metadata
from pycastle.session import RoleSession
from pycastle.session import RunKind
from pycastle.session.role import session_uuid_for_role_session_path


def _role_session_session_uuid(role_session: object) -> str:
    role_session_path = getattr(role_session, "path", None)
    if isinstance(role_session_path, Path):
        identity_uuid = session_uuid_for_role_session_path(role_session_path)
        if identity_uuid is not None:
            return identity_uuid
    legacy = getattr(role_session, "session_uuid", None)
    if callable(legacy):
        return legacy()
    raise AssertionError("Unable to derive role session identifier")


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


# ── ClaudeService.name ────────────────────────────────────────────────────────


def test_claude_service_name_is_claude():
    assert ClaudeService().name == "claude"


# ── ClaudeService.state_dir_relpath ──────────────────────────────────────────


def test_state_dir_relpath_without_namespace():
    result = ClaudeService().state_dir_relpath(AgentRole.IMPLEMENTER)
    assert result == RoleSession.provider_state_relpath_for(
        AgentRole.IMPLEMENTER, "claude"
    )


def test_state_dir_relpath_with_namespace():
    result = ClaudeService().state_dir_relpath(AgentRole.IMPROVE, "main")
    assert result == RoleSession.provider_state_relpath_for(
        AgentRole.IMPROVE, "claude", "main"
    )


def test_state_dir_relpath_empty_namespace_same_as_no_namespace():
    assert ClaudeService().state_dir_relpath(
        AgentRole.IMPLEMENTER, ""
    ) == ClaudeService().state_dir_relpath(AgentRole.IMPLEMENTER)


def test_state_dir_relpath_has_trailing_slash():
    result = ClaudeService().state_dir_relpath(AgentRole.IMPLEMENTER)
    assert result.endswith("/")


# ── ClaudeService.is_resumable ────────────────────────────────────────────────


def test_is_resumable_false_when_dir_absent(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    assert ClaudeService().is_resumable(state_dir) is False


def test_is_resumable_false_when_dir_empty(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    assert ClaudeService().is_resumable(state_dir) is False


def test_is_resumable_true_when_dir_has_files(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n")
    assert ClaudeService().is_resumable(state_dir) is True


def test_is_resumable_true_for_nested_files(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    subdir = state_dir / "projects"
    subdir.mkdir(parents=True)
    (subdir / "session.jsonl").write_text("{}\n")
    assert ClaudeService().is_resumable(state_dir) is True


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


@pytest.mark.parametrize("role", list(AgentRole))
@pytest.mark.parametrize(
    "flag", ["--disable-slash-commands", "--exclude-dynamic-system-prompt-sections"]
)
def test_build_command_includes_universal_flag_for_every_role(flag, role):
    assert flag in ClaudeService().build_command(role=role)


def test_build_command_planner_includes_tools_read_glob():
    cmd = ClaudeService().build_command(role=AgentRole.PLANNER)
    assert "--tools Read,Glob" in cmd


def test_build_command_divergence_resolver_includes_disallowed_tools():
    cmd = ClaudeService().build_command(role=AgentRole.DIVERGENCE_RESOLVER)
    assert '--disallowedTools "Edit Write NotebookEdit"' in cmd


def test_build_command_implementer_excludes_tools():
    cmd = ClaudeService().build_command(role=AgentRole.IMPLEMENTER)
    assert "--tools" not in cmd


def test_build_command_never_includes_bare_flag():
    for role in AgentRole:
        cmd = ClaudeService().build_command(role=role)
        assert "--bare" not in cmd, f"--bare should not appear for role {role}"


def test_build_command_includes_disallowed_tools_for_preflight_issue_role():
    cmd = ClaudeService().build_command(role=AgentRole.PREFLIGHT_ISSUE)
    assert '--disallowedTools "Edit Write NotebookEdit"' in cmd


def test_build_command_includes_disallowed_tools_for_improve_role():
    cmd = ClaudeService().build_command(role=AgentRole.IMPROVE)
    assert '--disallowedTools "Edit Write NotebookEdit"' in cmd


def test_build_command_includes_disallowed_tools_for_failure_report_role():
    cmd = ClaudeService().build_command(role=AgentRole.FAILURE_REPORT)
    assert '--disallowedTools "Edit Write NotebookEdit"' in cmd


def test_build_command_excludes_disallowed_tools_for_other_roles():
    for role in (
        AgentRole.PLANNER,
        AgentRole.IMPLEMENTER,
        AgentRole.REVIEWER,
        AgentRole.MERGER,
    ):
        cmd = ClaudeService().build_command(role=role)
        assert "--disallowedTools" not in cmd, (
            f"unexpected --disallowedTools for {role}"
        )


@pytest.mark.parametrize(
    ("role", "expects_strict_mcp"),
    [
        (AgentRole.IMPLEMENTER, True),
        (AgentRole.REVIEWER, True),
        (AgentRole.MERGER, True),
        (AgentRole.PREFLIGHT_ISSUE, True),
        (AgentRole.IMPROVE, True),
        (AgentRole.FAILURE_REPORT, True),
        (AgentRole.PLANNER, True),
        (AgentRole.DIVERGENCE_RESOLVER, True),
    ],
)
def test_build_command_strict_mcp_config_matches_role(role, expects_strict_mcp):
    cmd = ClaudeService().build_command(role=role)
    if expects_strict_mcp:
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config '{\"mcpServers\":{}}'" in cmd
    else:
        assert "--strict-mcp-config" not in cmd
        assert "--mcp-config" not in cmd


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


# ── ClaudeService.provider_session_state ─────────────────────────────────────


def test_provider_session_state_forced_resume_uses_preferred_session_id_without_files(
    tmp_path,
):
    service = ClaudeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)

    provider_session_state = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=None,
            has_resumable_provider_state=False,
            preferred_provider_session_id=_role_session_session_uuid(role_session),
            force_resume=True,
        )
    )

    assert provider_session_state.run_kind is RunKind.RESUME
    assert provider_session_state.provider_session_id == _role_session_session_uuid(
        role_session
    )
    assert provider_session_state.exact_transcript_match is False


def test_provider_session_state_fresh_without_preferred_session_id_uses_role_session_uuid(
    tmp_path,
):
    service = ClaudeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)

    provider_session_state = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=None,
            has_resumable_provider_state=False,
        )
    )

    assert provider_session_state.run_kind is RunKind.FRESH
    assert provider_session_state.provider_session_id == _role_session_session_uuid(
        role_session
    )
    assert provider_session_state.exact_transcript_match is False


def test_provider_session_state_exact_transcript_match_uses_preferred_session_id(
    tmp_path,
):
    service = ClaudeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    save_service_session_metadata(role_session.path, "claude", "preferred-id")

    provider_session_state = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/claude/",
            require_exact_transcript_match=True,
            preferred_provider_session_id="preferred-id",
        )
    )

    assert provider_session_state.run_kind is RunKind.RESUME
    assert provider_session_state.provider_session_id == "preferred-id"
    assert provider_session_state.exact_transcript_match is True


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
    assert any(isinstance(e, PromptTokens) and e.count == 1000 for e in events)


def test_run_yields_result_for_result_line():
    events = list(ClaudeService().run([_result_line("done")]))
    assert any(isinstance(e, Result) and e.text == "done" for e in events)


def test_run_yields_usage_limit_for_429_line():
    events = list(ClaudeService().run([_usage_limit_line()]))
    assert any(isinstance(e, UsageLimit) for e in events)


def test_run_uses_runtime_owned_provider_event_contracts():
    events = list(
        ClaudeService().run(
            [_assistant_with_usage_line("hi", 1000), _result_line("done")]
        )
    )

    assert events == [
        RuntimePromptTokens(count=1000),
        RuntimeAssistantTurn(text="hi"),
        RuntimeResult(text="done"),
    ]


def test_run_accepts_provider_session_callback_without_emitting_session_id():
    captured: list[str] = []

    events = list(
        ClaudeService().run(
            [_assistant_line("hello"), _result_line("done")],
            on_provider_session_id=captured.append,
        )
    )

    assert events == [AssistantTurn(text="hello"), Result(text="done")]
    assert captured == []


def test_run_stops_after_result():
    lines = [_result_line("done"), _assistant_line("after")]
    events = list(ClaudeService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_stops_after_usage_limit():
    lines = [_usage_limit_line(), _assistant_line("after")]
    events = list(ClaudeService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_stops_after_transient_error():
    lines = [
        json.dumps({"type": "result", "is_error": True, "api_error_status": 503}),
        _assistant_line("after"),
    ]

    events = list(ClaudeService().run(lines))

    assert events == [RuntimeTransientError(status_code=503, raw_message=lines[0])]


def test_run_stops_after_hard_error():
    lines = [
        json.dumps({"type": "result", "is_error": True, "api_error_status": 400}),
        _assistant_line("after"),
    ]

    events = list(ClaudeService().run(lines))

    assert events == [RuntimeHardError(status_code=400, raw_message=lines[0])]


def test_run_stops_after_credential_failure():
    lines = [
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 403,
                "result": (
                    "Your account has disabled Claude subscription access for "
                    "Claude Code."
                ),
            }
        ),
        _assistant_line("after"),
    ]

    events = list(ClaudeService().run(lines))

    assert events == [
        RuntimeCredentialFailure(
            raw_message=lines[0],
            service_name="claude",
            source_observations=(
                (
                    "json_event.result",
                    (
                        "Your account has disabled Claude subscription access for "
                        "Claude Code."
                    ),
                ),
            ),
            status_code=403,
        )
    ]


def test_run_skips_non_json_lines_silently():
    lines = ["not json", _result_line("done")]
    events = list(ClaudeService().run(lines))
    assert any(isinstance(e, Result) for e in events)


def test_run_usage_limit_carries_raw_message_when_result_is_not_string():
    line = json.dumps({"api_error_status": 429, "result": 42})
    events = list(ClaudeService().run([line]))
    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.raw_message == line


def test_run_usage_limit_carries_raw_message_when_result_has_no_reset_time():
    line = json.dumps({"api_error_status": 429, "result": "rate limit exceeded"})
    events = list(ClaudeService().run([line]))
    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.raw_message == line


def test_run_usage_limit_carries_raw_message_when_reset_time_hour_out_of_range():
    # hour=13 is out of range for 12-hour clock (1–12)
    line = json.dumps({"api_error_status": 429, "result": "limit resets 13:00am (UTC)"})
    events = list(ClaudeService().run([line]))
    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.raw_message == line


def test_run_usage_limit_has_no_raw_message_when_reset_time_parsed_successfully():
    line = json.dumps({"api_error_status": 429, "result": "limit resets 3:30pm (UTC)"})
    events = list(ClaudeService().run([line]))
    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.reset_time is not None
    assert limit.raw_message is None


def test_run_usage_limit_uses_runtime_owned_contract():
    line = json.dumps({"api_error_status": 429, "result": "rate limit exceeded"})

    events = list(ClaudeService().run([line]))

    assert events == [RuntimeUsageLimit(reset_time=None, raw_message="".join([line]))]


def test_run_transient_error_uses_runtime_owned_contract_when_status_missing():
    line = json.dumps({"type": "result", "is_error": True, "result": "temporary"})

    events = list(ClaudeService().run([line]))

    assert events == [RuntimeTransientError(status_code=None, raw_message=line)]


def test_run_hard_error_uses_runtime_owned_contract():
    line = json.dumps({"type": "result", "is_error": True, "api_error_status": 401})

    events = list(ClaudeService().run([line]))

    assert events == [RuntimeHardError(status_code=401, raw_message=line)]


def test_run_usage_limit_uses_local_timezone_from_now_local(monkeypatch):
    pacific = timezone(-timedelta(hours=7))
    monkeypatch.setattr(
        _time_module,
        "now_local",
        lambda: datetime(2026, 5, 27, 7, 0, tzinfo=pacific),
    )
    line = json.dumps({"api_error_status": 429, "result": "limit resets 3:30pm (UTC)"})

    events = list(ClaudeService().run([line]))

    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.reset_time == datetime(2026, 5, 27, 8, 30, tzinfo=pacific)
    assert limit.reset_time is not None
    assert limit.reset_time.utcoffset() == pacific.utcoffset(None)
