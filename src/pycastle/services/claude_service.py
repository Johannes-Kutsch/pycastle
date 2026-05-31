from __future__ import annotations

import dataclasses
import json
import shlex
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from ..agents.output_protocol import AgentRole
from .. import _time as _time_module
from ..session import SESSION_DIR_NAME, RunKind
from ..session.service_resume_identity import is_exact_resumable_service_session
from .flag_profiles import flag_profile_for
from .agent_service import (
    AssistantTurn,
    HardError,
    ParsedTurn,
    PromptTokens,
    Result,
    TransientError,
    UsageLimit,
)
from .provider_session_state import ProviderSessionState, ProviderSessionStateRequest
from ._wake_time import compute_wake_time
from .reset_time_parser import parse_claude_reset_time


# ── private account pool ──────────────────────────────────────────────────────


@dataclasses.dataclass
class _Account:
    name: str
    token: str
    exhausted_until: datetime | None = None


class _AccountPool:
    def __init__(self, accounts: list[tuple[str, str]]) -> None:
        if not accounts:
            raise ValueError("ClaudeService requires at least one account")
        self._accounts: list[_Account] = [
            _Account(name=n, token=t) for n, t in accounts
        ]

    def _is_exhausted(self, acc: _Account, now: datetime) -> bool:
        return acc.exhausted_until is not None and acc.exhausted_until > now

    def pick(self, now: datetime | None = None) -> tuple[str, str]:
        now = now or _time_module.now_local()
        for acc in self._accounts:
            if not self._is_exhausted(acc, now):
                return acc.name, acc.token
        raise RuntimeError("No available Claude accounts")

    def mark_exhausted(
        self, token: str, reset_time: datetime | None, now: datetime | None = None
    ) -> None:
        now = now or _time_module.now_local()
        wake, _ = compute_wake_time(reset_time, now)
        for acc in self._accounts:
            if acc.token == token:
                acc.exhausted_until = wake
                return

    def mark_permanently_exhausted(self, token: str) -> str | None:
        for acc in self._accounts:
            if acc.token == token:
                acc.exhausted_until = _PERMANENT_EXHAUSTION_WAKE
                return acc.name
        return None

    def has_available(self, now: datetime | None = None) -> bool:
        now = now or _time_module.now_local()
        return any(not self._is_exhausted(a, now) for a in self._accounts)

    def earliest_wake_time(self) -> datetime:
        wakes = [
            a.exhausted_until for a in self._accounts if a.exhausted_until is not None
        ]
        if not wakes:
            raise RuntimeError("No exhausted accounts")
        return min(wakes)

    def names(self) -> list[str]:
        return [a.name for a in self._accounts]


_SUBSCRIPTION_ACCESS_DENIAL_PHRASE = (
    "disabled Claude subscription access for Claude Code"
)
_PERMANENT_EXHAUSTION_WAKE = datetime(9999, 12, 31, 23, 59, tzinfo=timezone.utc)


def _is_subscription_access_denial(
    status: object, result: object, is_error: object
) -> bool:
    return (
        is_error is True
        and status == 403
        and isinstance(result, str)
        and _SUBSCRIPTION_ACCESS_DENIAL_PHRASE in result
    )


def _classify_line(line: str) -> list[ParsedTurn]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []

    if obj.get("api_error_status") == 429:
        reset_time = parse_claude_reset_time(obj.get("result"))
        raw = line if reset_time is None else None
        return [UsageLimit(reset_time=reset_time, raw_message=raw)]

    if _is_subscription_access_denial(
        obj.get("api_error_status"), obj.get("result"), obj.get("is_error")
    ):
        denial_message = obj.get("result")
        return [
            UsageLimit(
                reset_time=None,
                raw_message=denial_message if isinstance(denial_message, str) else None,
                is_permanent=True,
            )
        ]

    if obj.get("is_error") and obj.get("type") == "result":
        status = obj.get("api_error_status")
        if status is None or (isinstance(status, int) and status >= 500):
            return [
                TransientError(
                    status_code=status if isinstance(status, int) else None,
                    raw_message=line,
                )
            ]
        if isinstance(status, int) and 400 <= status < 500:
            return [HardError(status_code=status, raw_message=line)]
        return []

    if obj.get("type") == "assistant":
        msg = obj.get("message") or {}
        content = msg.get("content") or []
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    parts.append(text)
        turn_text = "\n\n".join(parts) if parts else None

        usage = msg.get("usage") or {}
        tokens: int | None = None
        if usage:
            total = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
            )
            if total > 0:
                tokens = total

        events: list[ParsedTurn] = []
        if tokens is not None:
            events.append(PromptTokens(count=tokens))
        if turn_text is not None:
            events.append(AssistantTurn(text=turn_text))
        return events

    if obj.get("type") == "result" and not obj.get("is_error"):
        r = obj.get("result")
        if isinstance(r, str):
            return [Result(text=r)]

    return []


class ClaudeService:
    def __init__(self, accounts: list[tuple[str, str]] | None = None) -> None:
        self._pool: _AccountPool | None = (
            _AccountPool(accounts) if accounts is not None else None
        )
        self._current_token: str | None = None

    @property
    def name(self) -> str:
        return "claude"

    def is_available(self, now: datetime | None = None) -> bool:
        if self._pool is None:
            return True
        return self._pool.has_available(now=now)

    def next_wake_time(self) -> datetime:
        if self._pool is None:
            raise RuntimeError("ClaudeService.next_wake_time called with no pool")
        return self._pool.earliest_wake_time()

    def mark_exhausted(
        self, reset_time: datetime | None, *, _now: datetime | None = None
    ) -> None:
        if self._pool is not None and self._current_token is not None:
            self._pool.mark_exhausted(self._current_token, reset_time, now=_now)

    def mark_permanently_exhausted(self) -> str | None:
        if self._pool is None or self._current_token is None:
            return None
        return self._pool.mark_permanently_exhausted(self._current_token)

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        if namespace:
            return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/claude/"
        return f"{SESSION_DIR_NAME}/{role.value}/claude/"

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(f.is_file() for f in state_dir.rglob("*"))

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState:
        exact_transcript_match = False
        run_kind = (
            RunKind.RESUME
            if request.force_resume or request.has_resumable_provider_state
            else RunKind.FRESH
        )
        provider_session_id = (
            request.preferred_provider_session_id or request.role_session.session_uuid()
        )
        if (
            request.require_exact_transcript_match
            and request.has_resumable_provider_state
            and run_kind is RunKind.RESUME
        ):
            exact_transcript_match = is_exact_resumable_service_session(
                request.role_session,
                self.name,
                provider_session_id=provider_session_id,
                provider_state_dir=request.provider_state_dir,
            )
        return ProviderSessionState(
            run_kind=run_kind,
            provider_session_id=provider_session_id,
            state_dir_relpath=request.state_dir_relpath,
            state_dir_path=request.provider_state_dir,
            exact_transcript_match=exact_transcript_match,
        )

    def account_names(self) -> list[str]:
        if self._pool is None:
            return []
        return self._pool.names()

    def valid_models(self) -> frozenset[str]:
        return frozenset({"haiku", "sonnet", "opus"})

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high", "xhigh", "max"})

    def build_command(
        self,
        role: AgentRole = AgentRole.IMPLEMENTER,
        model: str = "",
        effort: str = "",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> str:
        profile = flag_profile_for(role)
        flags = (
            "--verbose --dangerously-skip-permissions --output-format stream-json -p -"
            " --disable-slash-commands --exclude-dynamic-system-prompt-sections"
        )
        if profile.tools is not None:
            flags += f" --tools {shlex.quote(profile.tools)}"
        if profile.disallowed_tools:
            flags += f' --disallowedTools "{" ".join(profile.disallowed_tools)}"'
        if profile.strict_mcp:
            flags += " --strict-mcp-config --mcp-config '{\"mcpServers\":{}}'"
        if model:
            flags += f" --model {model}"
        if effort:
            flags += f" --effort {effort}"
        if session_uuid:
            if run_kind == RunKind.RESUME:
                flags += f" --resume {shlex.quote(session_uuid)}"
            else:
                flags += f" --session-id {shlex.quote(session_uuid)}"
        return f"claude {flags} < /tmp/.pycastle_prompt"

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        if token is None and self._pool is not None:
            _, self._current_token = self._pool.pick()
            token = self._current_token
        elif token is not None:
            self._current_token = token
        env: dict[str, str] = {}
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        if state_dir_container_path:
            env["CLAUDE_CONFIG_DIR"] = state_dir_container_path
        return env

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        for line in lines:
            for event in _classify_line(line):
                yield event
                if isinstance(event, (Result, UsageLimit, TransientError, HardError)):
                    return
