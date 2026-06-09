import json
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import _time as _time_module
from .roles import AgentRole
from .session import RunKind


class WorkInvocationLog:
    def __init__(self, log: BinaryIO) -> None:
        self._log = log

    def append_provider_chunk(self, provider_bytes: bytes) -> None:
        self._log.write(provider_bytes)
        self._log.flush()


class LogicalAgentInvocationLog:
    def __init__(self, owner: "AgentInvocationLog", *, log_path: Path) -> None:
        self._owner = owner
        self.log_path = log_path

    def open_work_invocation(
        self,
        *,
        role: AgentRole,
        run_kind: RunKind,
        session_uuid: str | None,
        prompt: str,
    ) -> AbstractContextManager[WorkInvocationLog]:
        return self._owner.open_work_invocation(
            log_path=self.log_path,
            role=role,
            run_kind=run_kind,
            session_uuid=session_uuid,
            prompt=prompt,
        )

    def append_work_invocation(
        self,
        *,
        role: AgentRole,
        run_kind: RunKind,
        session_uuid: str | None,
        prompt: str,
        provider_bytes: bytes,
    ) -> None:
        self._owner.append_work_invocation(
            log_path=self.log_path,
            role=role,
            run_kind=run_kind,
            session_uuid=session_uuid,
            prompt=prompt,
            provider_bytes=provider_bytes,
        )


class AgentInvocationLog:
    def __init__(
        self,
        *,
        now_local: Callable[[], datetime] | None = None,
    ) -> None:
        self._now_local = _time_module.now_local if now_local is None else now_local

    def reserve(self, *, agent_name: str, effective_logs_dir: Path) -> Path:
        effective_logs_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")
        timestamp = self._now_local().strftime("%Y%m%dT%H%M")
        stem = f"{slug}-{timestamp}"
        for suffix in ["", *[f"-{n}" for n in range(2, 10_000)]]:
            path = effective_logs_dir / f"{stem}{suffix}.log"
            try:
                with open(path, "xb"):
                    pass
                return path
            except FileExistsError:
                continue
        raise RuntimeError(f"could not reserve unique agent log path for {stem}")

    def start_logical_session(
        self,
        *,
        agent_name: str,
        effective_logs_dir: Path,
    ) -> LogicalAgentInvocationLog:
        return LogicalAgentInvocationLog(
            self,
            log_path=self.reserve(
                agent_name=agent_name,
                effective_logs_dir=effective_logs_dir,
            ),
        )

    @contextmanager
    def open_work_invocation(
        self,
        *,
        log_path: Path,
        role: AgentRole,
        run_kind: RunKind,
        session_uuid: str | None,
        prompt: str,
    ) -> Iterator[WorkInvocationLog]:
        with open(log_path, "ab") as log:
            separator = self._separator_for_next_invocation(log_path)
            if separator:
                log.write(separator)
            log.write(
                json.dumps(
                    {
                        "type": "pycastle_input",
                        "role": role.value,
                        "run_kind": run_kind.value,
                        "session_uuid": session_uuid,
                        "prompt": prompt,
                    }
                ).encode()
                + b"\n"
            )
            log.flush()
            yield WorkInvocationLog(log)

    def append_work_invocation(
        self,
        *,
        log_path: Path,
        role: AgentRole,
        run_kind: RunKind,
        session_uuid: str | None,
        prompt: str,
        provider_bytes: bytes,
    ) -> None:
        with self.open_work_invocation(
            log_path=log_path,
            role=role,
            run_kind=run_kind,
            session_uuid=session_uuid,
            prompt=prompt,
        ) as log:
            log.append_provider_chunk(provider_bytes)

    def _separator_for_next_invocation(self, log_path: Path) -> bytes:
        if not log_path.exists() or log_path.stat().st_size == 0:
            return b""
        with open(log_path, "rb") as existing_log:
            existing_log.seek(-1, 2)
            return b"\n\n" if existing_log.read(1) != b"\n" else b"\n"
