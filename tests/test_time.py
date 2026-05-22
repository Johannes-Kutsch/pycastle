import json
from datetime import datetime, timezone

import pytest

import pycastle._time as _time_module
from pycastle._time import now_local
from pycastle.agents.output_protocol import AgentRole, process_stream
from pycastle.errors import UsageLimitError


def test_now_local_returns_aware_datetime():
    result = now_local()
    assert result.tzinfo is not None


def test_now_local_returns_local_timezone():
    result = now_local()
    local_now = datetime.now().astimezone()
    assert result.tzinfo == local_now.tzinfo


def _usage_limit_line(text: str) -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": text,
        }
    )


def _freeze_now(monkeypatch: pytest.MonkeyPatch, frozen: datetime) -> None:
    monkeypatch.setattr(_time_module, "now_local", lambda: frozen)


def test_usage_limit_reset_time_is_aware_datetime(monkeypatch):
    frozen = datetime(2026, 5, 4, 8, 0, tzinfo=timezone.utc).astimezone()
    _freeze_now(monkeypatch, frozen)
    with pytest.raises(UsageLimitError) as exc_info:
        process_stream(
            [_usage_limit_line("resets May 7, 11:30am (UTC)")],
            on_turn=lambda t: None,
            role=AgentRole.IMPLEMENTER,
        )
    err = exc_info.value
    assert err.reset_time is not None
    assert err.reset_time.tzinfo is not None
