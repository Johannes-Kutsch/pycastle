import asyncio
import json
import queue
import re
import shlex
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..agents.output_protocol import AgentOutput, AgentRole, process_stream_from_events
from ..config import Config
from .docker_session import DockerSession
from ..errors import AgentTimeoutError, DockerError
from ..services.agent_service import AgentService
from ..session_resume import RunKind
from ..status_display import PlainStatusDisplay


class ContainerRunner:
    def __init__(
        self,
        name: str,
        session: DockerSession,
        model: str = "",
        effort: str = "",
        status_display=None,
        *,
        cfg: Config,
        service: AgentService,
    ) -> None:
        self.name = name
        self._session = session
        self.model = model
        self.effort = effort
        self._cfg = cfg
        self._service = service
        self._status_display = (
            status_display if status_display is not None else PlainStatusDisplay()
        )
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M")
        self._log_path = self._cfg.logs_dir / f"{slug}-{ts}.log"

    @property
    def log_path(self) -> Path:
        return self._log_path

    async def setup(self, git_name: str, git_email: str, work_body: str = "") -> None:
        self._cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._session.__enter__)
        await loop.run_in_executor(
            None,
            self._session.exec_simple,
            f"git config --global user.name {shlex.quote(git_name)}",
        )
        await loop.run_in_executor(
            None,
            self._session.exec_simple,
            f"git config --global user.email {shlex.quote(git_email)}",
        )
        await loop.run_in_executor(
            None,
            self._session.exec_simple,
            "pip install -e '.[dev]' || pip install -r requirements.txt",
        )

    async def preflight(
        self, checks: list[tuple[str, str]]
    ) -> list[tuple[str, str, str]]:
        loop = asyncio.get_running_loop()
        failures: list[tuple[str, str, str]] = []
        total = len(checks)
        for i, (check_name, command) in enumerate(checks, 1):
            self._status_display.update_phase(
                self.name, f"Running {check_name} ({i}/{total})"
            )
            try:
                await loop.run_in_executor(None, self._session.exec_simple, command)
            except DockerError as exc:
                failures.append((check_name, command, str(exc)))
        return failures

    async def work(
        self,
        role: AgentRole,
        prompt: str,
        *,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> AgentOutput:
        self._status_display.update_phase(self.name, "Work")
        loop = asyncio.get_running_loop()

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        def on_tokens(tokens: int) -> None:
            self._status_display.update_tokens(self.name, tokens)

        return await loop.run_in_executor(
            None,
            lambda: self._run_streaming(
                role, prompt, on_turn, on_tokens, run_kind, session_uuid
            ),
        )

    def _run_streaming(
        self,
        role: AgentRole,
        prompt: str,
        on_turn: Callable[[str], None],
        on_tokens: Callable[[int], None] | None = None,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> AgentOutput:
        self._session.write_file(prompt, "/tmp/.pycastle_prompt")
        command = self._service.build_command(
            model=self.model,
            effort=self.effort,
            run_kind=run_kind,
            session_uuid=session_uuid,
        )
        chunks = self._session.exec_stream(command)
        input_record = {
            "type": "pycastle_input",
            "role": role.value,
            "run_kind": run_kind.value,
            "session_uuid": session_uuid,
            "prompt": prompt,
        }

        q: queue.Queue[bytes | object] = queue.Queue()
        sentinel = object()

        def _feed() -> None:
            try:
                for chunk in chunks:
                    q.put(chunk)
            finally:
                q.put(sentinel)

        threading.Thread(target=_feed, daemon=True).start()

        try:
            with open(self._log_path, "wb") as log:
                log.write(json.dumps(input_record).encode() + b"\n")
                log.flush()

                def _lines():
                    line_buf = ""
                    while True:
                        try:
                            chunk = q.get(timeout=self._cfg.idle_timeout)
                        except queue.Empty:
                            raise AgentTimeoutError(
                                f"Agent idle for more than {self._cfg.idle_timeout}s"
                            )
                        if chunk is sentinel:
                            if line_buf:
                                yield line_buf
                            return
                        assert isinstance(chunk, bytes)
                        log.write(chunk)
                        log.flush()
                        self._status_display.reset_idle_timer(self.name)
                        line_buf += chunk.decode("utf-8", errors="replace")
                        while "\n" in line_buf:
                            line, line_buf = line_buf.split("\n", 1)
                            yield line

                return process_stream_from_events(
                    self._service.run(_lines()), on_turn, role, on_tokens
                )
        finally:
            try:
                self._session.exec_simple("rm -f /tmp/.pycastle_prompt")
            except Exception:
                pass
