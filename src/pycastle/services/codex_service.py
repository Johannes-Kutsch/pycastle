from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import _time as _time_module
from ..agents.output_protocol import AgentRole
from ..agents.output_protocol import AgentOutputProtocolError
from ..session import ProviderRunState, SESSION_DIR_NAME, RunKind
from ..session.service_resume_identity import ServiceResumeIdentityStore
from .agent_service import (
    AssistantTurn,
    HardError,
    ParsedTurn,
    PromptTokens,
    TransientError,
    UsageLimit,
)
from ._wake_time import compute_wake_time
from .flag_profiles import AgentToolPolicyGroup, tool_policy_group_for
from .reset_time_parser import ResetTimeSyntaxMode, parse_reset_time

_log = logging.getLogger(__name__)

_USAGE_LIMIT_SUBSTRING = "You've hit your usage limit"

_UNAUTHORIZED_RE = re.compile(
    r"\b(?:401|unauthorized|missing bearer|basic authentication)\b",
    re.IGNORECASE,
)
_HTTP_STATUS_RE = re.compile(r"\bstatus\s+(?P<status>\d{3})\b", re.IGNORECASE)


def _recover_rollout_thread_id(state_dir: Path | None) -> str | None:
    if state_dir is None:
        return None
    sessions_dir = state_dir / "sessions"
    if not sessions_dir.is_dir():
        return None

    found: set[str] = set()
    for rollout in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            lines = rollout.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "thread.started":
                continue
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                found.add(thread_id.strip())

    return next(iter(found)) if len(found) == 1 else None


def _classify_error_message(message: str) -> HardError | TransientError | None:
    if _UNAUTHORIZED_RE.search(message):
        return HardError(status_code=401, raw_message=message)

    match = _HTTP_STATUS_RE.search(message)
    if match is None:
        return None

    status = int(match.group("status"))
    if status >= 500:
        return TransientError(status_code=status, raw_message=message)
    if 400 <= status < 500:
        return HardError(status_code=status, raw_message=message)
    return None


def _extract_usage_limit(message: str) -> UsageLimit | None:
    """Return a UsageLimit if message contains the usage-limit substring."""
    if _USAGE_LIMIT_SUBSTRING not in message:
        return None
    reset_time = parse_reset_time(
        message, ResetTimeSyntaxMode.TRY_AGAIN_UTC_OPTIONAL_DATE
    )
    raw = message if reset_time is None else None
    return UsageLimit(reset_time=reset_time, raw_message=raw)


@dataclasses.dataclass(frozen=True)
class CodexPromptTokensContract:
    exact_live_extractor: Callable[[dict[str, Any]], int | None] | None = None
    require_exact_live: bool = False

    def extract_exact_live(self, event: dict[str, Any]) -> int | None:
        if self.exact_live_extractor is None:
            return None
        return self.exact_live_extractor(event)

    @classmethod
    def unsupported(cls) -> "CodexPromptTokensContract":
        return cls()


@dataclasses.dataclass
class CodexService:
    prompt_tokens_contract: CodexPromptTokensContract = dataclasses.field(
        default_factory=CodexPromptTokensContract.unsupported
    )
    _exhausted_until: datetime | None = dataclasses.field(default=None, init=False)

    @property
    def name(self) -> str:
        return "codex"

    def is_available(self, now: datetime | None = None) -> bool:
        if self._exhausted_until is None:
            return True
        now = now or _time_module.now_local()
        return now >= self._exhausted_until

    def next_wake_time(self) -> datetime:
        if self._exhausted_until is None:
            raise RuntimeError("CodexService.next_wake_time called when not exhausted")
        return self._exhausted_until

    def mark_exhausted(
        self, reset_time: datetime | None, *, _now: datetime | None = None
    ) -> None:
        now = _now or _time_module.now_local()
        wake, _ = compute_wake_time(reset_time, now)
        if wake.tzinfo is None:
            wake = wake.replace(tzinfo=timezone.utc)
        self._exhausted_until = wake

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        if namespace:
            return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/codex/"
        return f"{SESSION_DIR_NAME}/{role.value}/codex/"

    def is_resumable(self, state_dir: Path) -> bool:
        sessions_dir = state_dir / "sessions"
        if not sessions_dir.is_dir():
            return False
        return any(sessions_dir.rglob("rollout-*.jsonl"))

    def resolve_provider_run_state(
        self,
        role_session: ServiceResumeIdentityStore,
        *,
        provider_state_dir: Path | None,
        has_resumable_provider_state: bool,
    ) -> ProviderRunState:
        if not has_resumable_provider_state:
            return ProviderRunState(RunKind.FRESH, None)

        provider_session_id = role_session.service_session_id(self.name)
        if provider_session_id is not None:
            return ProviderRunState(RunKind.RESUME, provider_session_id)

        provider_session_id = _recover_rollout_thread_id(provider_state_dir)
        if provider_session_id is None:
            return ProviderRunState(RunKind.FRESH, None)

        role_session.save_service_session_id(self.name, provider_session_id)
        return ProviderRunState(
            RunKind.RESUME,
            provider_session_id,
            persist_provider_session_id=True,
        )

    def has_exact_transcript_session(
        self,
        role_session: ServiceResumeIdentityStore,
        *,
        provider_run_state: ProviderRunState,
        provider_state_dir: Path | None,
    ) -> bool:
        metadata = role_session.service_session_metadata(self.name)
        return (
            provider_run_state.run_kind is RunKind.RESUME
            and not provider_run_state.persist_provider_session_id
            and metadata is not None
            and metadata["provider_session_id"]
            == provider_run_state.provider_session_id
            and _recover_rollout_thread_id(provider_state_dir)
            == provider_run_state.provider_session_id
        )

    def valid_models(self) -> frozenset[str]:
        return frozenset(
            {
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.3-codex",
                "gpt-5.3-codex-spark",
                "gpt-5.2",
            }
        )

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high", "xhigh"})

    def build_command(
        self,
        role: AgentRole = AgentRole.IMPLEMENTER,
        model: str = "",
        effort: str = "",
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> str:
        policy_group = tool_policy_group_for(role)
        if run_kind == RunKind.RESUME and session_uuid:
            parts = ["codex exec resume"]
        else:
            parts = ["codex exec"]
        if model:
            parts.append(f"-m {model}")
        if effort:
            parts.append(f"-c model_reasoning_effort={effort}")
        parts.append("-c approval_policy=never")
        if run_kind != RunKind.RESUME:
            if policy_group is AgentToolPolicyGroup.PARTIAL:
                parts.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                parts.append("--sandbox danger-full-access")
        elif policy_group is AgentToolPolicyGroup.PARTIAL:
            parts.append("--dangerously-bypass-approvals-and-sandbox")
        if run_kind == RunKind.RESUME and session_uuid:
            parts.append(session_uuid)
        parts += [
            "--json",
            "< /tmp/.pycastle_prompt",
        ]
        return " ".join(parts)

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        env: dict[str, str] = {"TZ": "UTC"}
        if state_dir_container_path:
            env["CODEX_HOME"] = state_dir_container_path
        return env

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        saw_exact_live_prompt_tokens = False
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            exact_live_prompt_tokens = self.prompt_tokens_contract.extract_exact_live(
                obj
            )
            if exact_live_prompt_tokens is not None:
                saw_exact_live_prompt_tokens = True
                yield PromptTokens(count=exact_live_prompt_tokens)

            event_type = obj.get("type")

            if event_type == "thread.started":
                thread_id = obj.get("thread_id")
                if thread_id and on_provider_session_id is not None:
                    on_provider_session_id(thread_id)
                continue

            if event_type == "item.completed":
                item = obj.get("item") or {}
                item_type = item.get("type")
                if item_type == "agent_message":
                    content = item.get("text")
                    if content is None:
                        content = item.get("content") or ""
                    if content:
                        yield AssistantTurn(text=content)
                continue

            if event_type == "turn.completed":
                if (
                    self.prompt_tokens_contract.require_exact_live
                    and not saw_exact_live_prompt_tokens
                ):
                    raise AgentOutputProtocolError(
                        "Codex exact live prompt-side telemetry missing from stream."
                    )
                return

            if event_type == "turn.failed":
                error = obj.get("error") or {}
                message = error.get("message") or ""
                limit = _extract_usage_limit(message)
                if limit is not None:
                    yield limit
                else:
                    classified = _classify_error_message(message)
                    if classified is not None:
                        yield classified
                    _log.warning("codex turn.failed: %s", message)
                return

            if event_type == "error":
                message = obj.get("message") or ""
                limit = _extract_usage_limit(message)
                if limit is not None:
                    yield limit
                else:
                    classified = _classify_error_message(message)
                    if classified is not None:
                        yield classified
                    _log.warning("codex error: %s", message)
                return
