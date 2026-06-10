import json
from datetime import datetime, timezone

from pycastle.agents.output_protocol import AgentRole
from pycastle.session import RunKind
from pycastle_agent_runtime.agent_log import AgentInvocationLog


def test_logical_session_reuses_one_reserved_log_for_multiple_work_invocations(
    tmp_path,
):
    fixed_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone()
    log = AgentInvocationLog(now_local=lambda: fixed_dt)

    session = log.start_logical_session(
        agent_name="implementer",
        effective_logs_dir=tmp_path,
    )
    session.append_work_invocation(
        role=AgentRole.IMPLEMENTER,
        run_kind=RunKind.FRESH,
        session_uuid="session-1",
        prompt="first prompt",
        provider_bytes=b'{"type":"result","result":"first"}',
    )
    session.append_work_invocation(
        role=AgentRole.REVIEWER,
        run_kind=RunKind.RESUME,
        session_uuid="session-2",
        prompt="second prompt",
        provider_bytes=b'{"type":"result","result":"second"}\n',
    )

    assert session.log_path == tmp_path / "implementer-20260517T1430.log"

    log_lines = session.log_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(log_lines[0]) == {
        "type": "agent_invocation",
        "role": "implementer",
        "run_kind": "fresh",
        "provider_session_id": "session-1",
        "prompt": "first prompt",
    }
    assert log_lines[1] == '{"type":"result","result":"first"}'
    assert log_lines[2] == ""
    assert json.loads(log_lines[3]) == {
        "type": "agent_invocation",
        "role": "reviewer",
        "run_kind": "resume",
        "provider_session_id": "session-2",
        "prompt": "second prompt",
    }
    assert log_lines[4] == '{"type":"result","result":"second"}'


def test_logical_sessions_keep_work_invocations_in_separate_agent_logs(tmp_path):
    fixed_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone()
    log = AgentInvocationLog(now_local=lambda: fixed_dt)

    first_session = log.start_logical_session(
        agent_name="implementer",
        effective_logs_dir=tmp_path,
    )
    second_session = log.start_logical_session(
        agent_name="implementer",
        effective_logs_dir=tmp_path,
    )

    first_session.append_work_invocation(
        role=AgentRole.IMPLEMENTER,
        run_kind=RunKind.FRESH,
        session_uuid="session-1",
        prompt="first prompt",
        provider_bytes=b'{"type":"result","result":"first"}\n',
    )
    second_session.append_work_invocation(
        role=AgentRole.REVIEWER,
        run_kind=RunKind.RESUME,
        session_uuid="session-2",
        prompt="second prompt",
        provider_bytes=b'{"type":"result","result":"second"}\n',
    )

    assert first_session.log_path == tmp_path / "implementer-20260517T1430.log"
    assert second_session.log_path == tmp_path / "implementer-20260517T1430-2.log"
    assert first_session.log_path.read_text(encoding="utf-8").count("first prompt") == 1
    assert (
        second_session.log_path.read_text(encoding="utf-8").count("second prompt") == 1
    )
    assert "second prompt" not in first_session.log_path.read_text(encoding="utf-8")
    assert "first prompt" not in second_session.log_path.read_text(encoding="utf-8")


def test_reserve_creates_empty_agent_log_with_slug_and_local_minute_timestamp(
    tmp_path,
):
    fixed_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone()

    log_path = AgentInvocationLog(now_local=lambda: fixed_dt).reserve(
        agent_name="Plan Agent",
        effective_logs_dir=tmp_path,
    )

    assert log_path.name == f"plan-agent-{fixed_dt.strftime('%Y%m%dT%H%M')}.log"
    assert log_path.parent == tmp_path
    assert log_path.exists()
    assert log_path.read_text() == ""


def test_reserve_uses_numeric_suffixes_starting_at_two_for_same_slug_and_minute(
    tmp_path,
):
    fixed_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone()
    log = AgentInvocationLog(now_local=lambda: fixed_dt)

    first_path = log.reserve(agent_name="Plan Agent", effective_logs_dir=tmp_path)
    second_path = log.reserve(agent_name="Plan Agent", effective_logs_dir=tmp_path)

    assert first_path.name == f"plan-agent-{fixed_dt.strftime('%Y%m%dT%H%M')}.log"
    assert second_path.name == f"plan-agent-{fixed_dt.strftime('%Y%m%dT%H%M')}-2.log"


def test_reserve_does_not_collide_across_different_local_minutes(tmp_path):
    timestamps = iter(
        [
            datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone(),
            datetime(2026, 5, 17, 14, 31, tzinfo=timezone.utc).astimezone(),
        ]
    )
    log = AgentInvocationLog(now_local=lambda: next(timestamps))

    first_path = log.reserve(agent_name="Plan Agent", effective_logs_dir=tmp_path)
    second_path = log.reserve(agent_name="Plan Agent", effective_logs_dir=tmp_path)

    assert first_path.name == "plan-agent-20260517T1430.log"
    assert second_path.name == "plan-agent-20260517T1431.log"


def test_reserve_uses_container_runner_slug_rules_in_missing_effective_logs_dir(
    tmp_path,
):
    fixed_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone()
    effective_logs_dir = tmp_path / "nested" / "logs"

    log_path = AgentInvocationLog(now_local=lambda: fixed_dt).reserve(
        agent_name="!!!",
        effective_logs_dir=effective_logs_dir,
    )

    assert log_path.parent == effective_logs_dir
    assert effective_logs_dir.is_dir()
    assert log_path.name == f"-{fixed_dt.strftime('%Y%m%dT%H%M')}.log"
    assert log_path.read_text() == ""


def test_first_invocation_appends_agent_invocation_header_then_raw_bytes(tmp_path):
    log = AgentInvocationLog()
    log_path = log.reserve(agent_name="implementer", effective_logs_dir=tmp_path)
    raw_bytes = b'{"type":"result","result":"done"}\n'

    log.append_work_invocation(
        log_path=log_path,
        role=AgentRole.IMPLEMENTER,
        run_kind=RunKind.FRESH,
        session_uuid="provider-session-123",
        prompt="solve issue",
        provider_bytes=raw_bytes,
    )

    header, rest = log_path.read_bytes().split(b"\n", 1)
    assert json.loads(header) == {
        "type": "agent_invocation",
        "role": "implementer",
        "run_kind": "fresh",
        "provider_session_id": "provider-session-123",
        "prompt": "solve issue",
    }
    assert rest == raw_bytes


def test_second_invocation_adds_one_blank_line_before_next_agent_invocation_header(
    tmp_path,
):
    log = AgentInvocationLog()
    log_path = log.reserve(agent_name="implementer", effective_logs_dir=tmp_path)

    log.append_work_invocation(
        log_path=log_path,
        role=AgentRole.IMPLEMENTER,
        run_kind=RunKind.FRESH,
        session_uuid="session-1",
        prompt="first prompt",
        provider_bytes=b'{"type":"result","result":"first"}',
    )
    log.append_work_invocation(
        log_path=log_path,
        role=AgentRole.REVIEWER,
        run_kind=RunKind.RESUME,
        session_uuid="session-2",
        prompt="second prompt",
        provider_bytes=b'{"type":"result","result":"second"}\n',
    )

    log_lines = log_path.read_text(encoding="utf-8").splitlines()

    assert json.loads(log_lines[0]) == {
        "type": "agent_invocation",
        "role": "implementer",
        "run_kind": "fresh",
        "provider_session_id": "session-1",
        "prompt": "first prompt",
    }
    assert log_lines[1] == '{"type":"result","result":"first"}'
    assert log_lines[2] == ""
    assert json.loads(log_lines[3]) == {
        "type": "agent_invocation",
        "role": "reviewer",
        "run_kind": "resume",
        "provider_session_id": "session-2",
        "prompt": "second prompt",
    }
    assert log_lines[4] == '{"type":"result","result":"second"}'


def test_reserve_uses_supplied_falsey_clock_callable(tmp_path):
    fixed_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone()

    class FalseyClock:
        def __call__(self) -> datetime:
            return fixed_dt

        def __bool__(self) -> bool:
            return False

    log_path = AgentInvocationLog(now_local=FalseyClock()).reserve(
        agent_name="Plan Agent",
        effective_logs_dir=tmp_path,
    )

    assert log_path.name == f"plan-agent-{fixed_dt.strftime('%Y%m%dT%H%M')}.log"
