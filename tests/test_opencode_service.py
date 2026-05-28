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
        "opencode run --format json --model opencode-go/kimi-k2.6 "
        '"$(cat /tmp/.pycastle_prompt)"'
    )
    assert resume == (
        "opencode run --format json --session session-123 "
        '--model opencode-go/kimi-k2.6 "$(cat /tmp/.pycastle_prompt)"'
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
