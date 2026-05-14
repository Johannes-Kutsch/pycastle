from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Iterator
from functools import lru_cache

from ..agent_output_protocol import _check_usage_limit, _extract_turn
from ..errors import ClaudeCliNotFoundError
from ..session_resume import RunKind
from .agent_service import AssistantTurn, ParsedTurn, Result, Tokens, UsageLimit

# claude CLI does not expose a list-models subcommand; this list is kept in sync manually.
_KNOWN_MODELS: tuple[str, ...] = (
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
)


@lru_cache(maxsize=1)
def _list_models() -> tuple[str, ...]:
    """Return known Claude model IDs, verifying the CLI is installed. Cached for the process lifetime."""
    if shutil.which("claude") is None:
        raise ClaudeCliNotFoundError(
            "claude CLI not found; ensure it is installed and on PATH"
        )
    return _KNOWN_MODELS


class ClaudeService:
    @property
    def name(self) -> str:
        return "claude"

    def list_models(self) -> tuple[str, ...]:
        return _list_models()

    def build_command(
        self,
        model: str = "",
        effort: str = "",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> str:
        flags = (
            "--verbose --dangerously-skip-permissions --output-format stream-json -p -"
        )
        if model:
            flags += f" --model {model}"
        if effort:
            flags += f" --effort {effort}"
        if session_uuid:
            if run_kind == RunKind.RESUME:
                flags += f" --resume {session_uuid}"
            else:
                flags += f" --session-id {session_uuid}"
        return f"claude {flags} < /tmp/.pycastle_prompt"

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        if state_dir_container_path:
            env["CLAUDE_CONFIG_DIR"] = state_dir_container_path
        return env

    def run(self, lines: Iterable[str]) -> Iterator[ParsedTurn]:
        for line in lines:
            usage_limit = _check_usage_limit(line)
            if usage_limit is not False:
                yield UsageLimit(reset_time=usage_limit)
                return
            turn, tokens = _extract_turn(line)
            if tokens is not None:
                yield Tokens(count=tokens)
            if turn is not None:
                yield AssistantTurn(text=turn)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                r = obj.get("result")
                if isinstance(r, str):
                    yield Result(text=r)
                    return
