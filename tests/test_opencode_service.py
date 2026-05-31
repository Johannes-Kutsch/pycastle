from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from pycastle.services.agent_service import AssistantTurn, Result
from pycastle.services.agent_service import HardError
from pycastle.services.agent_service import TransientError
from pycastle.services.agent_service import UsageLimit
from pycastle.services.provider_session_state import ProviderSessionStateRequest
from pycastle.agents.output_protocol import AgentRole
from pycastle.services.opencode_service import OpenCodeService
from pycastle.session import RoleSession, RunKind


def test_opencode_service_builds_json_commands_and_go_api_env() -> None:
    service = OpenCodeService(api_key="go-key")

    restricted = service.build_command(
        role=AgentRole.PLANNER,
        model="kimi-k2.6",
        effort="medium",
        run_kind=RunKind.FRESH,
        session_uuid=None,
    )
    partial = service.build_command(
        role=AgentRole.PREFLIGHT_ISSUE,
        model="kimi-k2.6",
        effort="medium",
        run_kind=RunKind.FRESH,
        session_uuid=None,
    )
    fresh = service.build_command(
        role=AgentRole.IMPLEMENTER,
        model="kimi-k2.6",
        effort="medium",
        run_kind=RunKind.FRESH,
        session_uuid=None,
    )
    resume = service.build_command(
        role=AgentRole.IMPLEMENTER,
        model="kimi-k2.6",
        effort="medium",
        run_kind=RunKind.RESUME,
        session_uuid="session-123",
    )
    env = service.build_env("/workspace/.pycastle-session/implementer/opencode")

    assert fresh == (
        "opencode run --format json --model opencode-go/kimi-k2.6 "
        '"$(cat /tmp/.pycastle_prompt)"'
    )
    assert restricted == fresh
    assert partial == fresh
    assert resume == (
        "opencode run --format json --session session-123 "
        '--model opencode-go/kimi-k2.6 "$(cat /tmp/.pycastle_prompt)"'
    )
    assert env["TZ"] == "UTC"
    assert env["OPENCODE_HOME"] == "/workspace/.pycastle-session/implementer/opencode"
    assert env["OPENCODE_GO_API_KEY"] == "go-key"
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    provider = config["provider"]["opencode-go"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "https://opencode.ai/zen/go/v1",
        "apiKey": "{env:OPENCODE_GO_API_KEY}",
    }
    assert "kimi-k2.6" in provider["models"]
    assert "deepseek-v4-flash" in provider["models"]


def test_opencode_service_surfaces_assistant_turns_final_result_and_session_id() -> (
    None
):
    service = OpenCodeService()
    session_ids: list[str] = []

    events = list(
        service.run(
            [
                (
                    '{"type":"text","timestamp":1,"sessionID":"sess_123",'
                    '"part":{"id":"part_1","sessionID":"sess_123","messageID":"msg_1",'
                    '"type":"text","text":"first assistant turn","time":{"start":1,"end":2}}}'
                ),
                (
                    '{"type":"text","timestamp":2,"sessionID":"sess_123",'
                    '"part":{"id":"part_2","sessionID":"sess_123","messageID":"msg_1",'
                    '"type":"text","text":"second assistant turn","time":{"start":2,"end":3}}}'
                ),
                (
                    '{"type":"session.status","timestamp":3,"sessionID":"sess_123",'
                    '"status":{"type":"idle"}}'
                ),
            ],
            on_provider_session_id=session_ids.append,
        )
    )

    assert session_ids == ["sess_123"]
    assert events == [
        AssistantTurn(text="first assistant turn"),
        AssistantTurn(text="second assistant turn"),
        Result(text="first assistant turn\n\nsecond assistant turn"),
    ]


def test_opencode_service_maps_usage_limit_errors_with_and_without_reset_time() -> None:
    service = OpenCodeService()

    parsed = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"sess_123","error":{'
                    '"name":"RateLimitError","data":{"message":"You have reached your '
                    'OpenCode Go usage limit. Try again at Apr 28th, 2026 9:02 PM.",'
                    '"statusCode":429,"isRetryable":true}}}'
                )
            ]
        )
    )
    raw = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"sess_123","error":{'
                    '"name":"RateLimitError","data":{"message":"You have reached your '
                    'OpenCode Go usage limit.","statusCode":429,"isRetryable":true}}}'
                )
            ]
        )
    )

    assert parsed == [
        UsageLimit(
            reset_time=datetime(2026, 4, 28, 21, 2, tzinfo=timezone.utc).astimezone(),
            raw_message=None,
        )
    ]
    assert raw == [
        UsageLimit(
            reset_time=None,
            raw_message="You have reached your OpenCode Go usage limit.",
        )
    ]


def test_opencode_service_leaves_malformed_explicit_retry_at_as_raw_message() -> None:
    service = OpenCodeService()

    events = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"sess_123","error":{'
                    '"name":"RateLimitError","data":{"message":"You have reached your '
                    'OpenCode Go usage limit. Try again at Apr 28th, 2026 0:02 PM.",'
                    '"statusCode":429,"isRetryable":true}}}'
                )
            ]
        )
    )

    assert events == [
        UsageLimit(
            reset_time=None,
            raw_message=(
                "You have reached your OpenCode Go usage limit. "
                "Try again at Apr 28th, 2026 0:02 PM."
            ),
        )
    ]


def test_opencode_service_maps_transient_and_hard_runtime_errors() -> None:
    service = OpenCodeService()

    transient = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"sess_123","error":{'
                    '"name":"InternalServerError","data":{"message":"temporary backend '
                    'failure","statusCode":503,"isRetryable":true}}}'
                )
            ]
        )
    )
    missing_status = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"sess_123","error":{'
                    '"name":"UnknownError","data":{"message":"connection dropped",'
                    '"isRetryable":true}}}'
                )
            ]
        )
    )
    hard = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"sess_123","error":{'
                    '"name":"AuthenticationError","data":{"message":"invalid api key",'
                    '"statusCode":401,"isRetryable":false}}}'
                )
            ]
        )
    )

    assert transient == [
        TransientError(status_code=503, raw_message="temporary backend failure")
    ]
    assert missing_status == [
        TransientError(status_code=None, raw_message="connection dropped")
    ]
    assert hard == [HardError(status_code=401, raw_message="invalid api key")]


def test_opencode_service_maps_missing_model_without_status_as_hard_error() -> None:
    service = OpenCodeService()

    events = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"sess_123","error":{'
                    '"name":"UnknownError","data":{"message":"Model not found: '
                    'opencode-go/deepseek-v4-flash. Did you mean: deepseek-v4-flash?"}}}'
                )
            ]
        )
    )

    assert events == [
        HardError(
            status_code=400,
            raw_message=(
                "Model not found: opencode-go/deepseek-v4-flash. "
                "Did you mean: deepseek-v4-flash?"
            ),
        )
    ]


def test_opencode_service_session_id_callback_fires_once_for_repeated_id() -> None:
    service = OpenCodeService()
    session_ids: list[str] = []

    list(
        service.run(
            [
                (
                    '{"type":"text","sessionID":"sess_1",'
                    '"part":{"type":"text","text":"a","time":{"start":1,"end":2}}}'
                ),
                (
                    '{"type":"text","sessionID":"sess_1",'
                    '"part":{"type":"text","text":"b","time":{"start":2,"end":3}}}'
                ),
                '{"type":"session.status","sessionID":"sess_1","status":{"type":"idle"}}',
            ],
            on_provider_session_id=session_ids.append,
        )
    )

    assert session_ids == ["sess_1"]


def test_opencode_service_exact_transcript_requires_metadata_saved_id_and_resumable_state_to_match(
    tmp_path,
) -> None:
    service = OpenCodeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.save_service_session_id("opencode", "sess-opencode-123")
    role_session.save_service_session_metadata("opencode", "sess-opencode-123")
    provider_state_dir = tmp_path / "selected-opencode-state"
    provider_state_dir.mkdir(parents=True)
    (provider_state_dir / "session_id").write_text(
        "sess-opencode-other",
        encoding="utf-8",
    )

    provider_session_state = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=True,
            require_exact_transcript_match=True,
        )
    )

    assert provider_session_state.exact_transcript_match is False


def test_opencode_service_resolves_resume_with_saved_session_id_when_state_is_resumable(
    tmp_path: Path,
) -> None:
    service = OpenCodeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.save_service_session_id("opencode", "sess-opencode-123")
    provider_state_dir = tmp_path / "selected-opencode-state"
    provider_state_dir.mkdir(parents=True)
    (provider_state_dir / "session_id").write_text(
        "sess-opencode-other",
        encoding="utf-8",
    )

    provider_session_state = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=True,
        )
    )

    assert provider_session_state.run_kind is RunKind.RESUME
    assert provider_session_state.provider_session_id == "sess-opencode-123"
    assert provider_session_state.persist_provider_session_id is False


def test_opencode_service_recovers_resume_from_selected_state_dir_without_saved_session_id(
    tmp_path: Path,
) -> None:
    service = OpenCodeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    provider_state_dir = tmp_path / "selected-opencode-state"
    provider_state_dir.mkdir(parents=True)
    (provider_state_dir / "session_id").write_text(
        "sess-opencode-123",
        encoding="utf-8",
    )

    provider_session_state = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=True,
        )
    )

    assert provider_session_state.run_kind is RunKind.RESUME
    assert provider_session_state.provider_session_id == "sess-opencode-123"
    assert provider_session_state.persist_provider_session_id is True
    assert role_session.service_session_id("opencode") == "sess-opencode-123"


def test_opencode_service_exact_transcript_recovers_saved_session_id_before_matching(
    tmp_path: Path,
) -> None:
    service = OpenCodeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.save_service_session_metadata("opencode", "sess-opencode-123")
    provider_state_dir = tmp_path / "selected-opencode-state"
    provider_state_dir.mkdir(parents=True)
    (provider_state_dir / "session_id").write_text(
        "sess-opencode-123",
        encoding="utf-8",
    )

    provider_session_state = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=True,
            require_exact_transcript_match=True,
        )
    )

    assert provider_session_state.run_kind is RunKind.RESUME
    assert provider_session_state.provider_session_id == "sess-opencode-123"
    assert provider_session_state.persist_provider_session_id is True
    assert provider_session_state.exact_transcript_match is True


def test_opencode_service_skips_malformed_json_and_non_dict_values() -> None:
    service = OpenCodeService()

    events = list(
        service.run(
            [
                "not json at all",
                '"just a string"',
                "[1, 2, 3]",
                (
                    '{"type":"text","sessionID":"s",'
                    '"part":{"type":"text","text":"valid","time":{"start":1,"end":2}}}'
                ),
                '{"type":"session.status","status":{"type":"idle"}}',
            ]
        )
    )

    assert events == [AssistantTurn(text="valid"), Result(text="valid")]


def test_opencode_service_skips_text_events_with_incomplete_part() -> None:
    service = OpenCodeService()

    events = list(
        service.run(
            [
                # part still in progress: time.end is null
                (
                    '{"type":"text","sessionID":"s",'
                    '"part":{"type":"text","text":"in-progress","time":{"start":1,"end":null}}}'
                ),
                # part missing entirely
                '{"type":"text","sessionID":"s"}',
                # part has wrong type
                (
                    '{"type":"text","sessionID":"s",'
                    '"part":{"type":"tool_use","text":"tool call","time":{"start":1,"end":2}}}'
                ),
                # valid completed part
                (
                    '{"type":"text","sessionID":"s",'
                    '"part":{"type":"text","text":"complete","time":{"start":1,"end":2}}}'
                ),
                '{"type":"session.status","status":{"type":"idle"}}',
            ]
        )
    )

    assert events == [AssistantTurn(text="complete"), Result(text="complete")]


def test_opencode_service_skips_whitespace_only_assistant_text() -> None:
    service = OpenCodeService()

    events = list(
        service.run(
            [
                (
                    '{"type":"text","sessionID":"s",'
                    '"part":{"type":"text","text":"   ","time":{"start":1,"end":2}}}'
                ),
                (
                    '{"type":"text","sessionID":"s",'
                    '"part":{"type":"text","text":"real content","time":{"start":2,"end":3}}}'
                ),
                '{"type":"session.status","status":{"type":"idle"}}',
            ]
        )
    )

    assert events == [
        AssistantTurn(text="real content"),
        Result(text="real content"),
    ]


def test_opencode_service_yields_no_result_when_idle_follows_no_assistant_turns() -> (
    None
):
    service = OpenCodeService()

    events = list(service.run(['{"type":"session.status","status":{"type":"idle"}}']))

    assert events == []


def test_opencode_service_parses_noon_and_midnight_reset_times() -> None:
    service = OpenCodeService()

    noon = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"s","error":{'
                    '"name":"RateLimitError","data":{"message":"Limit hit. '
                    'Try again at May 1st, 2026 12:00 PM.",'
                    '"statusCode":429}}}'
                )
            ]
        )
    )
    midnight = list(
        service.run(
            [
                (
                    '{"type":"error","timestamp":1,"sessionID":"s","error":{'
                    '"name":"RateLimitError","data":{"message":"Limit hit. '
                    'Try again at May 1st, 2026 12:00 AM.",'
                    '"statusCode":429}}}'
                )
            ]
        )
    )

    assert noon == [
        UsageLimit(
            reset_time=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).astimezone(),
            raw_message=None,
        )
    ]
    assert midnight == [
        UsageLimit(
            reset_time=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc).astimezone(),
            raw_message=None,
        )
    ]
