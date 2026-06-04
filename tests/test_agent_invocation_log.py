import json

from pycastle.agents.output_protocol import AgentRole
from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog
from pycastle.session import RunKind


def test_first_invocation_appends_pycastle_input_header_then_raw_bytes(tmp_path):
    log = AgentInvocationLog()
    log_path = log.reserve(agent_name="implementer", effective_logs_dir=tmp_path)
    raw_bytes = b'{"type":"result","result":"done"}\n'

    log.append_work_invocation(
        log_path=log_path,
        role=AgentRole.IMPLEMENTER,
        run_kind=RunKind.FRESH,
        session_uuid="session-123",
        prompt="solve issue",
        provider_bytes=raw_bytes,
    )

    header, rest = log_path.read_bytes().split(b"\n", 1)
    assert json.loads(header) == {
        "type": "pycastle_input",
        "role": "implementer",
        "run_kind": "fresh",
        "session_uuid": "session-123",
        "prompt": "solve issue",
    }
    assert rest == raw_bytes
