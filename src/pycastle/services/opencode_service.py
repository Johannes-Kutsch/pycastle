from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..agents.output_protocol import AgentRole
from ..session import SESSION_DIR_NAME, RunKind
from .agent_service import AssistantTurn, ParsedTurn, Result


@dataclasses.dataclass
class OpenCodeService:
    api_key: str | None = None

    @property
    def name(self) -> str:
        return "opencode"

    def build_command(
        self,
        role: AgentRole = AgentRole.IMPLEMENTER,
        model: str = "",
        effort: str = "",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> str:
        del role, effort
        parts = ["opencode run", "--format json"]
        if run_kind == RunKind.RESUME and session_uuid:
            parts.append(f"--session {session_uuid}")
        if model:
            parts.append(f"--model opencode-go/{model}")
        parts.append('"$(cat /tmp/.pycastle_prompt)"')
        return " ".join(parts)

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        del token
        env: dict[str, str] = {"TZ": "UTC"}
        if state_dir_container_path:
            env["OPENCODE_HOME"] = state_dir_container_path
        if self.api_key:
            env["OPENCODE_GO_API_KEY"] = self.api_key
        return env

    def run(
        self,
        lines: Iterable[str],
        on_thread_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        del on_thread_id
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "message" and isinstance(event.get("content"), str):
                yield AssistantTurn(text=event["content"])
            if event.get("type") == "result" and isinstance(event.get("content"), str):
                yield Result(text=event["content"])

    def is_available(self, now: datetime | None = None) -> bool:
        del now
        return True

    def next_wake_time(self) -> datetime:
        return datetime.now(timezone.utc)

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        del reset_time

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        if namespace:
            return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/opencode/"
        return f"{SESSION_DIR_NAME}/{role.value}/opencode/"

    def is_resumable(self, state_dir: Path) -> bool:
        return (state_dir / "session_id").is_file()

    def valid_models(self) -> frozenset[str]:
        return frozenset(
            {
                "deepseek-v4-flash",
                "deepseek-v4-pro",
                "glm-5",
                "glm-5.1",
                "hy3-preview",
                "kimi-k2.5",
                "kimi-k2.6",
                "mimo-v2-omni",
                "mimo-v2-pro",
                "mimo-v2.5",
                "mimo-v2.5-pro",
                "minimax-m2.5",
                "minimax-m2.7",
                "qwen3.5-plus",
                "qwen3.6-plus",
                "qwen3.7-max",
            }
        )

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})
