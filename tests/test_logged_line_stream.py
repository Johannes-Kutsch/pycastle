import json
import threading
from pathlib import Path

import pytest

from pycastle.errors import AgentTimeoutError
from pycastle.agents.output_protocol import AgentRole
from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog
from pycastle.infrastructure._logged_line_stream import (
    pycastle_input_compatibility_record,
    stream_logged_lines,
    stream_logged_work_lines,
)
from pycastle.runtime_session import RunKind


def _collect_stream(
    chunks, *, log_path: Path, idle_timeout: float = 1.0, input_record=None
):
    on_chunk_calls: list[str] = []
    lines = list(
        stream_logged_lines(
            chunks,
            log_path=log_path,
            input_record=input_record
            or pycastle_input_compatibility_record(prompt="prompt"),
            idle_timeout=idle_timeout,
            on_chunk=lambda: on_chunk_calls.append("chunk"),
        )
    )
    return lines, on_chunk_calls


def test_stream_logged_work_lines_handles_one_complete_invocation(tmp_path):
    logical_session = AgentInvocationLog().start_logical_session(
        agent_name="agent",
        effective_logs_dir=tmp_path,
    )
    on_chunk_calls: list[str] = []

    lines = list(
        stream_logged_work_lines(
            [b'{"type":"result","result":"done"}\n'],
            logical_session=logical_session,
            role=AgentRole.IMPLEMENTER,
            run_kind=RunKind.FRESH,
            session_uuid=None,
            prompt="prompt",
            idle_timeout=1.0,
            on_chunk=lambda: on_chunk_calls.append("chunk"),
        )
    )

    assert lines == ['{"type":"result","result":"done"}']
    assert on_chunk_calls == ["chunk"]
    assert logical_session.log_path.read_bytes() == (
        b'{"type": "agent_invocation", "role": "implementer", '
        b'"run_kind": "fresh", "provider_session_id": null, "prompt": "prompt"}\n'
        b'{"type":"result","result":"done"}\n'
    )


def test_stream_logged_work_lines_repeated_invocations_insert_one_blank_line_separator(
    tmp_path,
):
    logical_session = AgentInvocationLog().start_logical_session(
        agent_name="agent",
        effective_logs_dir=tmp_path,
    )

    list(
        stream_logged_work_lines(
            [b'{"type":"result","result":"first"}'],
            logical_session=logical_session,
            role=AgentRole.IMPLEMENTER,
            run_kind=RunKind.FRESH,
            session_uuid="session-1",
            prompt="first prompt",
            idle_timeout=1.0,
            on_chunk=lambda: None,
        )
    )

    list(
        stream_logged_work_lines(
            [b'{"type":"result","result":"second"}\n'],
            logical_session=logical_session,
            role=AgentRole.REVIEWER,
            run_kind=RunKind.RESUME,
            session_uuid="session-2",
            prompt="second prompt",
            idle_timeout=1.0,
            on_chunk=lambda: None,
        )
    )

    log_lines = logical_session.log_path.read_text(encoding="utf-8").splitlines()

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


def test_stream_logged_lines_logs_input_record_and_chunk_bytes(tmp_path):
    log_path = tmp_path / "agent.log"
    chunks = [b'{"type":"result"', b',"result":"done"}\n']

    lines, on_chunk_calls = _collect_stream(chunks, log_path=log_path)

    assert lines == ['{"type":"result","result":"done"}']
    assert on_chunk_calls == ["chunk", "chunk"]
    header, rest = log_path.read_bytes().split(b"\n", 1)
    assert json.loads(header) == {
        "type": "pycastle_input",
        "prompt": "prompt",
    }
    assert rest == b"".join(chunks)


def test_stream_logged_lines_writes_pycastle_input_only_via_compatibility_record(
    tmp_path,
):
    log_path = tmp_path / "agent.log"

    lines = list(
        stream_logged_lines(
            [b"done\n"],
            log_path=log_path,
            input_record=pycastle_input_compatibility_record(prompt="compat prompt"),
            idle_timeout=1.0,
            on_chunk=lambda: None,
        )
    )

    assert lines == ["done"]
    assert json.loads(log_path.read_text(encoding="utf-8").splitlines()[0]) == {
        "type": "pycastle_input",
        "prompt": "compat prompt",
    }


def test_stream_logged_lines_reports_each_provider_chunk_to_progress_callback(
    tmp_path,
):
    log_path = tmp_path / "agent.log"
    chunks = [b"first ", b"second\n"]
    reported_chunks: list[bytes] = []

    lines = list(
        stream_logged_lines(
            chunks,
            log_path=log_path,
            input_record=pycastle_input_compatibility_record(prompt="prompt"),
            idle_timeout=1.0,
            on_chunk=lambda chunk: reported_chunks.append(chunk),
        )
    )

    assert lines == ["first second"]
    assert reported_chunks == chunks


def test_stream_logged_lines_preserves_no_arg_progress_callback_without_signature(
    tmp_path,
):
    log_path = tmp_path / "agent.log"
    progress_state = {"pending": True}

    lines = list(
        stream_logged_lines(
            [b"done\n"],
            log_path=log_path,
            input_record=pycastle_input_compatibility_record(prompt="prompt"),
            idle_timeout=1.0,
            on_chunk=progress_state.clear,
        )
    )

    assert lines == ["done"]
    assert progress_state == {}


def test_stream_logged_lines_appends_new_record_after_blank_separator(tmp_path):
    log_path = tmp_path / "agent.log"
    log_path.write_bytes(b"previous line")

    lines, _ = _collect_stream([b"next line\n"], log_path=log_path)

    assert lines == ["next line"]
    log_bytes = log_path.read_bytes()
    assert log_bytes.startswith(b"previous line\n")
    log_lines = log_path.read_text(encoding="utf-8").split("\n")
    assert log_lines[0] == "previous line"
    assert log_lines[1] == ""
    assert json.loads(log_lines[2]) == {
        "type": "pycastle_input",
        "prompt": "prompt",
    }
    assert log_lines[3] == "next line"


def test_stream_logged_lines_yields_partial_final_line_without_newline(tmp_path):
    log_path = tmp_path / "agent.log"

    lines, on_chunk_calls = _collect_stream([b"partial"], log_path=log_path)

    assert lines == ["partial"]
    assert on_chunk_calls == ["chunk"]


def test_stream_logged_lines_yields_each_line_from_one_multiline_chunk_in_order(
    tmp_path,
):
    log_path = tmp_path / "agent.log"

    lines, on_chunk_calls = _collect_stream(
        [b"first line\nsecond line\nthird line\n"],
        log_path=log_path,
    )

    assert lines == ["first line", "second line", "third line"]
    assert on_chunk_calls == ["chunk"]


def test_stream_logged_lines_waits_for_terminating_newline_before_yielding_split_line(
    tmp_path,
):
    log_path = tmp_path / "agent.log"
    chunks = [b"split", b" line", b"\nnext line\n"]

    lines, on_chunk_calls = _collect_stream(chunks, log_path=log_path)

    assert lines == ["split line", "next line"]
    assert on_chunk_calls == ["chunk", "chunk", "chunk"]


def test_stream_logged_lines_decodes_split_utf8_sequences_with_replacement_and_preserves_log_bytes(
    tmp_path,
):
    log_path = tmp_path / "agent.log"
    chunks = [b"\xf0\x9f", b"\x98\x80\nbad:\xff\n"]

    lines, on_chunk_calls = _collect_stream(chunks, log_path=log_path)

    assert lines == ["😀", "bad:\ufffd"]
    assert on_chunk_calls == ["chunk", "chunk"]
    _header, rest = log_path.read_bytes().split(b"\n", 1)
    assert rest == b"".join(chunks)


def test_stream_logged_lines_raises_agent_timeout_error_after_idle_timeout(tmp_path):
    log_path = tmp_path / "agent.log"
    event = threading.Event()

    chunks = (event.wait() or b"never" for _ in range(1))

    with pytest.raises(AgentTimeoutError, match="Agent idle for more than 0.05s"):
        list(
            stream_logged_lines(
                chunks,
                log_path=log_path,
                input_record=pycastle_input_compatibility_record(prompt="stalled"),
                idle_timeout=0.05,
                on_chunk=lambda: None,
            )
        )

    first_line = log_path.read_bytes().split(b"\n")[0]
    assert json.loads(first_line) == {
        "type": "pycastle_input",
        "prompt": "stalled",
    }


def test_stream_logged_lines_does_not_report_progress_for_stream_completion(tmp_path):
    log_path = tmp_path / "agent.log"
    on_chunk_calls: list[str] = []

    lines = list(
        stream_logged_lines(
            [],
            log_path=log_path,
            input_record=pycastle_input_compatibility_record(prompt="done"),
            idle_timeout=1.0,
            on_chunk=lambda: on_chunk_calls.append("chunk"),
        )
    )

    assert lines == []
    assert on_chunk_calls == []


def test_stream_logged_lines_resets_idle_wait_after_each_provider_chunk(tmp_path):
    log_path = tmp_path / "agent.log"
    on_chunk_calls: list[str] = []

    def delayed_chunks():
        yield b"first line\n"
        threading.Event().wait(0.03)
        yield b"second line\n"
        threading.Event().wait(0.08)
        yield b"never"

    with pytest.raises(AgentTimeoutError, match="Agent idle for more than 0.05s"):
        list(
            stream_logged_lines(
                delayed_chunks(),
                log_path=log_path,
                input_record=pycastle_input_compatibility_record(prompt="prompt"),
                idle_timeout=0.05,
                on_chunk=lambda: on_chunk_calls.append("chunk"),
            )
        )

    assert on_chunk_calls == ["chunk", "chunk"]
