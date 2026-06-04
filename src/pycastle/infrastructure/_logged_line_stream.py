import codecs
import inspect
import json
import queue
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import cast

from ..agents.output_protocol import AgentRole
from ..errors import AgentTimeoutError
from ..session.resume import RunKind


def _build_progress_notifier(
    on_chunk: Callable[[], None] | Callable[[bytes], None],
) -> Callable[[bytes], None]:
    try:
        params = inspect.signature(on_chunk).parameters.values()
    except (TypeError, ValueError):
        no_arg_callback = cast(Callable[[], None], on_chunk)
        return lambda _chunk: no_arg_callback()

    accepts_chunk = any(
        param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for param in params
    )
    if accepts_chunk:
        return cast(Callable[[bytes], None], on_chunk)

    no_arg_callback = cast(Callable[[], None], on_chunk)
    return lambda _chunk: no_arg_callback()


def stream_logged_lines(
    chunks: Iterable[bytes],
    *,
    log_path: Path,
    input_record: Mapping[str, object],
    idle_timeout: float,
    on_chunk: Callable[[], None] | Callable[[bytes], None],
) -> Iterator[str]:
    q: queue.Queue[bytes | object] = queue.Queue()
    sentinel = object()
    notify_progress = _build_progress_notifier(on_chunk)

    def _feed() -> None:
        try:
            for chunk in chunks:
                q.put(chunk)
        finally:
            q.put(sentinel)

    threading.Thread(target=_feed, daemon=True).start()

    separator = b""
    if log_path.exists() and log_path.stat().st_size > 0:
        with open(log_path, "rb") as existing_log:
            existing_log.seek(-1, 2)
            separator = b"\n\n" if existing_log.read(1) != b"\n" else b"\n"

    with open(log_path, "ab") as log:
        if separator:
            log.write(separator)
        log.write(json.dumps(input_record).encode() + b"\n")
        log.flush()

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        line_buf = ""
        while True:
            try:
                chunk = q.get(timeout=idle_timeout)
            except queue.Empty as exc:
                raise AgentTimeoutError(
                    f"Agent idle for more than {idle_timeout}s"
                ) from exc
            if chunk is sentinel:
                line_buf += decoder.decode(b"", final=True)
                if line_buf:
                    yield line_buf
                return
            assert isinstance(chunk, bytes)
            log.write(chunk)
            log.flush()
            notify_progress(chunk)
            line_buf += decoder.decode(chunk)
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                yield line


def stream_logged_work_lines(
    chunks: Iterable[bytes],
    *,
    log_path: Path,
    role: AgentRole,
    run_kind: RunKind,
    session_uuid: str | None,
    prompt: str,
    idle_timeout: float,
    on_chunk: Callable[[], None] | Callable[[bytes], None],
) -> Iterator[str]:
    return stream_logged_lines(
        chunks,
        log_path=log_path,
        input_record={
            "type": "pycastle_input",
            "role": role.value,
            "run_kind": run_kind.value,
            "session_uuid": session_uuid,
            "prompt": prompt,
        },
        idle_timeout=idle_timeout,
        on_chunk=on_chunk,
    )
