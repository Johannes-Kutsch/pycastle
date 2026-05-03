import queue
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from .agent_output_protocol import AgentOutput, AgentRole, process_stream
from .errors import AgentTimeoutError


class WorkStream:
    def __init__(
        self,
        chunks: Iterable[bytes],
        log_path: Path,
        idle_timeout: float,
        on_chunk: Callable[[], None],
    ) -> None:
        self._chunks = chunks
        self._log_path = log_path
        self._idle_timeout = idle_timeout
        self._on_chunk = on_chunk

    def run(
        self,
        role: AgentRole,
        on_turn: Callable[[str], None],
        usage_limit_patterns: tuple[str, ...],
    ) -> AgentOutput:
        q: queue.Queue = queue.Queue()
        _sentinel = object()

        def _feed() -> None:
            try:
                for chunk in self._chunks:
                    q.put(chunk)
            finally:
                q.put(_sentinel)

        threading.Thread(target=_feed, daemon=True).start()

        log = open(self._log_path, "wb")  # noqa: WPS515
        try:

            def _lines():
                line_buf = ""
                while True:
                    try:
                        chunk = q.get(timeout=self._idle_timeout)
                    except queue.Empty:
                        raise AgentTimeoutError(
                            f"Agent idle for more than {self._idle_timeout}s"
                        )
                    if chunk is _sentinel:
                        return
                    log.write(chunk)
                    log.flush()
                    self._on_chunk()
                    text = chunk.decode("utf-8", errors="replace")
                    line_buf += text
                    while "\n" in line_buf:
                        line, line_buf = line_buf.split("\n", 1)
                        yield line

            return process_stream(_lines(), on_turn, role, usage_limit_patterns)
        finally:
            log.close()
