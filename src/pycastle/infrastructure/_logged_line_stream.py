import json
import queue
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path

from ..errors import AgentTimeoutError


def stream_logged_lines(
    chunks: Iterable[bytes],
    *,
    log_path: Path,
    input_record: Mapping[str, object],
    idle_timeout: float,
    on_chunk: Callable[[], None],
) -> Iterator[str]:
    q: queue.Queue[bytes | object] = queue.Queue()
    sentinel = object()

    def _feed() -> None:
        try:
            for chunk in chunks:
                q.put(chunk)
        finally:
            q.put(sentinel)

    threading.Thread(target=_feed, daemon=True).start()

    with open(log_path, "ab") as log:
        if log_path.stat().st_size > 0:
            log.write(b"\n")
        log.write(json.dumps(input_record).encode() + b"\n")
        log.flush()

        line_buf = ""
        while True:
            try:
                chunk = q.get(timeout=idle_timeout)
            except queue.Empty as exc:
                raise AgentTimeoutError(
                    f"Agent idle for more than {idle_timeout}s"
                ) from exc
            if chunk is sentinel:
                if line_buf:
                    yield line_buf
                return
            assert isinstance(chunk, bytes)
            log.write(chunk)
            log.flush()
            on_chunk()
            line_buf += chunk.decode("utf-8", errors="replace")
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                yield line
