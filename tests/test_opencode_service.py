from __future__ import annotations

from datetime import datetime
from datetime import timezone

from pycastle.services.agent_service import AssistantTurn, Result
from pycastle.services.agent_service import HardError
from pycastle.services.agent_service import TransientError
from pycastle.services.agent_service import UsageLimit
from pycastle.agents.output_protocol import AgentRole
from pycastle.services.opencode_service import OpenCodeService
from pycastle.session import RunKind


def test_opencode_service_builds_json_commands_and_go_api_env() -> None:
    service = OpenCodeService(api_key="go-key")

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
        "opencode run --format json --model kimi-k2.6 "
        '"$(cat /tmp/.pycastle_prompt)"'
    )
    assert resume == (
        "opencode run --format json --session session-123 "
        '--model kimi-k2.6 "$(cat /tmp/.pycastle_prompt)"'
    )
    assert env == {
        "TZ": "UTC",
        "OPENCODE_HOME": "/workspace/.pycastle-session/implementer/opencode",
        "OPENCODE_GO_API_KEY": "go-key",
    }


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
            on_thread_id=session_ids.append,
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
            on_thread_id=session_ids.append,
        )
    )

    assert session_ids == ["sess_1"]


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
