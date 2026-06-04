from datetime import datetime, timezone


def test_reserve_creates_empty_log_file_with_slug_and_local_minute_timestamp(
    tmp_path,
):
    from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog

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
    from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog

    fixed_dt = datetime(2026, 5, 17, 14, 30, tzinfo=timezone.utc).astimezone()
    log = AgentInvocationLog(now_local=lambda: fixed_dt)

    first_path = log.reserve(agent_name="Plan Agent", effective_logs_dir=tmp_path)
    second_path = log.reserve(agent_name="Plan Agent", effective_logs_dir=tmp_path)

    assert first_path.name == f"plan-agent-{fixed_dt.strftime('%Y%m%dT%H%M')}.log"
    assert second_path.name == f"plan-agent-{fixed_dt.strftime('%Y%m%dT%H%M')}-2.log"


def test_reserve_does_not_collide_across_different_local_minutes(tmp_path):
    from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog

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
    from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog

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
