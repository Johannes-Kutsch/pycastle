from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pycastle import _time as _time_module
from pycastle.agents.output_protocol import AgentOutputProtocolError, AgentRole
from pycastle.services import CodexService
from pycastle.services.codex_service import CodexPromptTokensContract
from pycastle.services.agent_service import (
    AssistantTurn,
    HardError,
    PromptTokens,
    UnsupportedTokens,
    TransientError,
    UsageLimit,
)
from pycastle.session import RoleSession, RunKind


# ── helpers ───────────────────────────────────────────────────────────────────


def _thread_started(thread_id: str = "thread-abc123") -> str:
    return json.dumps({"type": "thread.started", "thread_id": thread_id})


def _item_completed(item_type: str, content: str = "") -> str:
    item: dict = {"type": item_type}
    if content:
        item["content"] = content
    return json.dumps({"type": "item.completed", "item": item})


def _item_completed_text(item_type: str, text: str = "") -> str:
    item: dict = {"type": item_type}
    if text:
        item["text"] = text
    return json.dumps({"type": "item.completed", "item": item})


def _turn_completed(
    input_tokens: int = 100,
    cached_tokens: int = 50,
    output_tokens: int = 200,
    reasoning_tokens: int = 10,
) -> str:
    return json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "cached_tokens": cached_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
            },
        }
    )


def _turn_failed(message: str) -> str:
    return json.dumps({"type": "turn.failed", "error": {"message": message}})


def _error_line(message: str) -> str:
    return json.dumps({"type": "error", "message": message})


# ── CodexService.build_command: fresh session ─────────────────────────────────


def test_build_command_fresh_starts_with_codex_exec():
    cmd = CodexService().build_command(
        model="codex-mini-latest", effort="high", run_kind=RunKind.FRESH
    )
    assert cmd.startswith("codex exec ")


def test_build_command_fresh_does_not_include_resume():
    cmd = CodexService().build_command(run_kind=RunKind.FRESH)
    assert "resume" not in cmd


def test_build_command_fresh_includes_model():
    cmd = CodexService().build_command(
        model="codex-mini-latest", run_kind=RunKind.FRESH
    )
    assert "-m codex-mini-latest" in cmd


def test_build_command_fresh_includes_effort():
    cmd = CodexService().build_command(effort="high", run_kind=RunKind.FRESH)
    assert "-c model_reasoning_effort=high" in cmd


def test_build_command_fresh_includes_approval_policy():
    cmd = CodexService().build_command(run_kind=RunKind.FRESH)
    assert "-c approval_policy=never" in cmd


def test_build_command_fresh_includes_sandbox():
    cmd = CodexService().build_command(run_kind=RunKind.FRESH)
    assert "--sandbox danger-full-access" in cmd


def test_build_command_fresh_improve_uses_partial_role_bypass_policy():
    cmd = CodexService().build_command(role=AgentRole.IMPROVE, run_kind=RunKind.FRESH)
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "-c approval_policy=never" in cmd
    assert "--sandbox" not in cmd


def test_build_command_role_policies_keep_restricted_partial_and_full_distinct():
    planner_cmd = CodexService().build_command(
        role=AgentRole.PLANNER, run_kind=RunKind.FRESH
    )
    partial_cmd = CodexService().build_command(
        role=AgentRole.PREFLIGHT_ISSUE, run_kind=RunKind.FRESH
    )
    full_cmd = CodexService().build_command(
        role=AgentRole.IMPLEMENTER, run_kind=RunKind.FRESH
    )

    assert "--dangerously-bypass-approvals-and-sandbox" not in planner_cmd
    assert "--sandbox danger-full-access" in planner_cmd

    assert "--dangerously-bypass-approvals-and-sandbox" in partial_cmd
    assert "--sandbox" not in partial_cmd

    assert "--dangerously-bypass-approvals-and-sandbox" not in full_cmd
    assert "--sandbox danger-full-access" in full_cmd


def test_build_command_fresh_includes_json_flag():
    cmd = CodexService().build_command(run_kind=RunKind.FRESH)
    assert "--json" in cmd


def test_build_command_fresh_includes_prompt_redirect():
    cmd = CodexService().build_command(run_kind=RunKind.FRESH)
    assert "< /tmp/.pycastle_prompt" in cmd


# ── CodexService.build_command: resume session ───────────────────────────────


def test_build_command_resume_includes_exec_resume():
    cmd = CodexService().build_command(
        run_kind=RunKind.RESUME, session_uuid="thread-xyz"
    )
    assert "codex exec resume " in cmd
    assert "thread-xyz" in cmd


def test_build_command_resume_includes_thread_id():
    cmd = CodexService().build_command(
        run_kind=RunKind.RESUME, session_uuid="thread-abc"
    )
    assert "thread-abc" in cmd


def test_build_command_resume_includes_model():
    cmd = CodexService().build_command(
        model="codex-mini-latest",
        run_kind=RunKind.RESUME,
        session_uuid="thread-xyz",
    )
    assert "-m codex-mini-latest" in cmd


def test_build_command_resume_includes_approval_policy():
    cmd = CodexService().build_command(
        run_kind=RunKind.RESUME, session_uuid="thread-xyz"
    )
    assert "-c approval_policy=never" in cmd


def test_build_command_resume_excludes_sandbox():
    cmd = CodexService().build_command(
        run_kind=RunKind.RESUME, session_uuid="thread-xyz"
    )
    assert "--sandbox" not in cmd


def test_build_command_resume_improve_keeps_partial_role_bypass_before_session_id():
    cmd = CodexService().build_command(
        role=AgentRole.IMPROVE,
        run_kind=RunKind.RESUME,
        session_uuid="thread-xyz",
    )
    assert (
        "codex exec resume -c approval_policy=never "
        "--dangerously-bypass-approvals-and-sandbox thread-xyz" in cmd
    )
    assert "-c approval_policy=never" in cmd
    assert "--sandbox" not in cmd


# ── CodexService.build_env ────────────────────────────────────────────────────


def test_build_env_sets_tz_utc():
    env = CodexService().build_env()
    assert env.get("TZ") == "UTC"


def test_build_env_sets_codex_home_from_state_dir():
    env = CodexService().build_env(
        state_dir_container_path="/home/agent/workspace/.pycastle-session/planner/codex"
    )
    assert (
        env.get("CODEX_HOME") == "/home/agent/workspace/.pycastle-session/planner/codex"
    )


def test_build_env_omits_codex_home_without_state_dir():
    env = CodexService().build_env()
    assert "CODEX_HOME" not in env


def test_build_env_does_not_set_token_env_var():
    env = CodexService().build_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CODEX_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_env_without_state_dir_contains_only_tz():
    env = CodexService().build_env()
    assert set(env.keys()) == {"TZ"}


def test_build_env_with_state_dir_contains_tz_and_codex_home():
    env = CodexService().build_env(
        state_dir_container_path="/home/agent/workspace/.pycastle-session/planner/codex"
    )
    assert set(env.keys()) == {"TZ", "CODEX_HOME"}


# ── CodexService.state_dir_relpath ────────────────────────────────────────────


def test_state_dir_relpath_without_namespace():
    result = CodexService().state_dir_relpath(AgentRole.IMPLEMENTER)
    assert result == ".pycastle-session/implementer/codex/"


def test_state_dir_relpath_with_namespace():
    result = CodexService().state_dir_relpath(AgentRole.IMPROVE, "main")
    assert result == ".pycastle-session/improve/main/codex/"


def test_state_dir_relpath_has_trailing_slash():
    result = CodexService().state_dir_relpath(AgentRole.IMPLEMENTER)
    assert result is not None
    assert result.endswith("/")


def test_state_dir_relpath_empty_namespace_same_as_no_namespace():
    assert CodexService().state_dir_relpath(
        AgentRole.IMPLEMENTER, ""
    ) == CodexService().state_dir_relpath(AgentRole.IMPLEMENTER)


# ── CodexService.is_resumable ─────────────────────────────────────────────────


def test_is_resumable_false_when_dir_absent(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    assert CodexService().is_resumable(state_dir) is False


def test_is_resumable_false_when_only_auth_json_present(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    state_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text("{}")
    assert CodexService().is_resumable(state_dir) is False


def test_is_resumable_false_when_sessions_dir_absent(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    state_dir.mkdir(parents=True)
    assert CodexService().is_resumable(state_dir) is False


def test_is_resumable_true_when_rollout_jsonl_exists(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text("{}\n")
    assert CodexService().is_resumable(state_dir) is True


def test_is_resumable_true_when_nested_rollout_jsonl_exists(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    nested_sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    nested_sessions_dir.mkdir(parents=True)
    (nested_sessions_dir / "rollout-001.jsonl").write_text("{}\n")
    assert CodexService().is_resumable(state_dir) is True


def test_is_resumable_false_when_sessions_dir_empty(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    assert CodexService().is_resumable(state_dir) is False


def test_is_resumable_false_when_sessions_has_non_rollout_files(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "other-file.json").write_text("{}")
    assert CodexService().is_resumable(state_dir) is False


def test_has_exact_transcript_session_false_for_recovered_codex_identity(tmp_path):
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    role_session.save_service_session_metadata("codex", "thread-abc")

    provider_run_state = service.resolve_provider_run_state(
        role_session,
        provider_state_dir=state_dir,
        has_resumable_provider_state=True,
    )

    assert provider_run_state.persist_provider_session_id is True
    assert (
        service.has_exact_transcript_session(
            role_session,
            provider_run_state=provider_run_state,
            provider_state_dir=state_dir,
        )
        is False
    )


# ── CodexService.run: JSONL parsing ──────────────────────────────────────────


def test_run_yields_assistant_turn_for_agent_message():
    lines = [
        _thread_started(),
        _item_completed("agent_message", "Hello world"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert any(isinstance(e, AssistantTurn) and e.text == "Hello world" for e in events)


def test_run_yields_assistant_turn_from_current_agent_message_text_field():
    lines = [
        _thread_started(),
        _item_completed_text("agent_message", '<plan>{"issues": []}</plan>'),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert any(
        isinstance(e, AssistantTurn) and e.text == '<plan>{"issues": []}</plan>'
        for e in events
    )


def test_run_ignores_turn_completed_usage_without_exact_live_prompt_tokens():
    lines = [
        _thread_started(),
        _item_completed("agent_message", "Hi"),
        _turn_completed(
            input_tokens=100, cached_tokens=50, output_tokens=200, reasoning_tokens=10
        ),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, (PromptTokens, UnsupportedTokens)) for e in events)


def test_run_ignores_current_turn_completed_usage_fields_without_exact_live_prompt_tokens():
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 50,
                "output_tokens": 200,
                "reasoning_output_tokens": 10,
            },
        }
    )
    events = list(CodexService().run([line]))
    assert not any(isinstance(e, (PromptTokens, UnsupportedTokens)) for e in events)


def test_run_requires_exact_live_prompt_tokens_when_contract_promises_them():
    service = CodexService(
        prompt_tokens_contract=CodexPromptTokensContract(
            exact_live_extractor=lambda event: event.get("exact_prompt_tokens"),
            require_exact_live=True,
        )
    )
    lines = [
        _thread_started(),
        _item_completed("agent_message", "Hi"),
        _turn_completed(),
    ]
    with pytest.raises(AgentOutputProtocolError, match="telemetry missing"):
        list(service.run(lines))


def test_run_stops_after_turn_completed():
    lines = [
        _thread_started(),
        _item_completed("agent_message", "First"),
        _turn_completed(),
        _item_completed("agent_message", "Second"),
    ]
    events = list(CodexService().run(lines))
    assistant_turns = [e for e in events if isinstance(e, AssistantTurn)]
    assert len(assistant_turns) == 1
    assert assistant_turns[0].text == "First"


def test_run_skips_reasoning_items():
    lines = [
        _item_completed("reasoning"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_skips_command_execution_items():
    lines = [
        _item_completed("command_execution"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_skips_file_change_items():
    lines = [
        _item_completed("file_change"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_skips_mcp_tool_call_items():
    lines = [
        _item_completed("mcp_tool_call"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_skips_turn_started_events():
    lines = [
        json.dumps({"type": "turn.started"}),
        _item_completed("agent_message", "Hi"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert any(isinstance(e, AssistantTurn) for e in events)


def test_run_skips_item_started_events():
    lines = [
        json.dumps({"type": "item.started", "item": {"type": "agent_message"}}),
        _item_completed("agent_message", "Hi"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert sum(1 for e in events if isinstance(e, AssistantTurn)) == 1


def test_run_skips_item_updated_events():
    lines = [
        json.dumps(
            {
                "type": "item.updated",
                "item": {"type": "agent_message", "content": "partial"},
            }
        ),
        _item_completed("agent_message", "Hi"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert sum(1 for e in events if isinstance(e, AssistantTurn)) == 1


def test_run_skips_non_json_lines():
    lines = [
        "not json at all",
        _item_completed("agent_message", "Hi"),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert any(isinstance(e, AssistantTurn) for e in events)


def test_run_thread_started_surfaces_thread_id():
    captured: list[str] = []
    lines = [
        _thread_started("thread-test-id"),
        _item_completed("agent_message", "Hi"),
        _turn_completed(),
    ]
    list(CodexService().run(lines, on_thread_id=captured.append))
    assert captured == ["thread-test-id"]


def test_run_thread_started_yields_no_event():
    lines = [
        _thread_started(),
        _turn_completed(),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)
    tokens = [e for e in events if isinstance(e, (PromptTokens, UnsupportedTokens))]
    assert len(tokens) == 0


# ── CodexService.run: usage-limit parsing ────────────────────────────────────


_SAME_DAY_LIMIT_MSG = (
    "You've hit your usage limit. Please wait or try again at 3:30 PM."
)
_CROSS_DAY_LIMIT_MSG = (
    "You've hit your usage limit. Please wait or try again at March 15th, 2026 3:30 PM."
)
_DEGRADED_LIMIT_MSG = "You've hit your usage limit. Try again later."


def test_run_yields_usage_limit_on_turn_failed(monkeypatch):
    frozen = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)
    lines = [_turn_failed(_SAME_DAY_LIMIT_MSG)]
    events = list(CodexService().run(lines))
    assert any(isinstance(e, UsageLimit) for e in events)


def test_run_yields_usage_limit_on_error_event(monkeypatch):
    frozen = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)
    lines = [_error_line(_SAME_DAY_LIMIT_MSG)]
    events = list(CodexService().run(lines))
    assert any(isinstance(e, UsageLimit) for e in events)


def test_run_same_day_limit_parses_reset_time(monkeypatch):
    frozen = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)
    lines = [_turn_failed(_SAME_DAY_LIMIT_MSG)]
    events = list(CodexService().run(lines))
    usage_events = [e for e in events if isinstance(e, UsageLimit)]
    assert len(usage_events) == 1
    assert usage_events[0].reset_time is not None
    rt = usage_events[0].reset_time
    assert rt.tzinfo is not None
    utc_rt = rt.astimezone(timezone.utc)
    assert utc_rt.hour == 15
    assert utc_rt.minute == 30


def test_run_cross_day_limit_parses_reset_time(monkeypatch):
    frozen = datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)
    lines = [_turn_failed(_CROSS_DAY_LIMIT_MSG)]
    events = list(CodexService().run(lines))
    usage_events = [e for e in events if isinstance(e, UsageLimit)]
    assert len(usage_events) == 1
    assert usage_events[0].reset_time is not None
    rt = usage_events[0].reset_time
    assert rt.tzinfo is not None
    utc_rt = rt.astimezone(timezone.utc)
    assert utc_rt.year == 2026
    assert utc_rt.month == 3
    assert utc_rt.day == 15
    assert utc_rt.hour == 15
    assert utc_rt.minute == 30


def test_run_degraded_limit_has_none_reset_time():
    lines = [_turn_failed(_DEGRADED_LIMIT_MSG)]
    events = list(CodexService().run(lines))
    usage_events = [e for e in events if isinstance(e, UsageLimit)]
    assert len(usage_events) == 1
    assert usage_events[0].reset_time is None


def test_run_stops_after_usage_limit(monkeypatch):
    frozen = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)
    lines = [
        _turn_failed(_SAME_DAY_LIMIT_MSG),
        _item_completed("agent_message", "should not appear"),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)


def test_run_non_usage_limit_turn_failed_logs_and_returns():
    lines = [
        _turn_failed("Some other error"),
        _item_completed("agent_message", "should not appear"),
    ]
    events = list(CodexService().run(lines))
    assert not any(isinstance(e, AssistantTurn) for e in events)
    assert not any(isinstance(e, UsageLimit) for e in events)


def test_run_error_event_unauthorized_yields_hard_error():
    message = (
        "Reconnecting... 2/5 (unexpected status 401 Unauthorized: "
        "Missing bearer or basic authentication in header)"
    )
    events = list(CodexService().run([_error_line(message)]))
    assert events == [HardError(status_code=401, raw_message=message)]


def test_run_turn_failed_4xx_status_yields_hard_error():
    message = "unexpected status 403 Forbidden"
    events = list(CodexService().run([_turn_failed(message)]))
    assert events == [HardError(status_code=403, raw_message=message)]


def test_run_error_event_5xx_status_yields_transient_error():
    message = "unexpected status 529 Overloaded"
    events = list(CodexService().run([_error_line(message)]))
    assert events == [TransientError(status_code=529, raw_message=message)]


# ── CodexService exhaustion state ────────────────────────────────────────────


def test_is_available_true_by_default():
    assert CodexService().is_available() is True


def test_is_available_false_after_mark_exhausted_with_reset_time():
    svc = CodexService()
    reset = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    svc.mark_exhausted(reset)
    now = datetime(2026, 5, 18, 12, 1, tzinfo=timezone.utc)
    assert svc.is_available(now=now) is False


def test_is_available_true_after_reset_time_plus_2min():
    svc = CodexService()
    reset = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    svc.mark_exhausted(reset)
    after = datetime(2026, 5, 18, 12, 3, tzinfo=timezone.utc)
    assert svc.is_available(now=after) is True


def test_next_wake_time_returns_wake_after_mark_exhausted_with_reset():
    svc = CodexService()
    reset = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    svc.mark_exhausted(reset)
    assert svc.next_wake_time() is not None


def test_is_available_accepts_tz_aware_now():
    svc = CodexService()
    reset = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    svc.mark_exhausted(reset)
    now_aware = datetime(2026, 5, 18, 12, 3, tzinfo=timezone.utc)
    assert svc.is_available(now=now_aware) is True


def test_next_wake_time_returns_wake_after_mark_exhausted_without_reset():
    svc = CodexService()
    now = datetime(2026, 5, 18, 12, 15, tzinfo=timezone.utc)
    svc.mark_exhausted(None, _now=now)
    assert svc.next_wake_time() is not None


# ── CodexService.valid_efforts ────────────────────────────────────────────────


def test_valid_efforts_returns_frozenset():
    assert isinstance(CodexService().valid_efforts(), frozenset)


def test_valid_efforts_contains_expected_values():
    efforts = CodexService().valid_efforts()
    assert efforts == frozenset({"low", "medium", "high", "xhigh"})


# ── CodexService.name ─────────────────────────────────────────────────────────


def test_codex_service_name_is_codex():
    assert CodexService().name == "codex"


# ── Service registry ──────────────────────────────────────────────────────────


def test_service_registry_recognizes_codex():
    from pycastle.services import CodexService as _CodexService

    svc = _CodexService()
    assert svc.name == "codex"


# ── UsageLimit raw_message ────────────────────────────────────────────────────


def test_run_turn_failed_usage_limit_carries_raw_message_when_reset_time_unparseable():
    message = "You've hit your usage limit. Contact support."
    events = list(CodexService().run([_turn_failed(message)]))
    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.raw_message == message


def test_run_error_event_usage_limit_carries_raw_message_when_reset_time_unparseable():
    message = "You've hit your usage limit. No reset time available."
    events = list(CodexService().run([_error_line(message)]))
    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.raw_message == message


def test_run_turn_failed_usage_limit_has_no_raw_message_when_reset_time_parsed(
    monkeypatch,
):
    frozen = datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)
    message = "You've hit your usage limit. Please wait or try again at 3:30 PM"
    events = list(CodexService().run([_turn_failed(message)]))
    limit = next(e for e in events if isinstance(e, UsageLimit))
    assert limit.reset_time is not None
    assert limit.raw_message is None
