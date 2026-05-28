from __future__ import annotations

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
