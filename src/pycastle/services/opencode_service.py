from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from .. import _time as _time_module
from ..agents.output_protocol import AgentRole
from ..session import SESSION_DIR_NAME, RunKind
from ..session._provider_session_sidecars import load_state_dir_provider_session_id
from ..session.service_resume_identity import is_exact_resumable_service_session
from .agent_service import (
    AssistantTurn,
    HardError,
    ParsedTurn,
    Result,
    TransientError,
    UsageLimit,
)
from .provider_session_state import ProviderSessionState, ProviderSessionStateRequest
from ._wake_time import compute_wake_time
from .flag_profiles import AgentToolPolicyGroup, tool_policy_group_for
from .reset_time_parser import ResetTimeSyntaxMode, parse_reset_time

_OPENCODE_GO_PROVIDER_ID = "opencode-go"
_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
_OPENCODE_GO_MODELS = (
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
)


@dataclasses.dataclass(frozen=True)
class _OpenCodePolicyMapping:
    cli_args: tuple[str, ...] = ()
    supports_policy_controls: bool = False


_OPENCODE_POLICY_MAPPINGS = {
    AgentToolPolicyGroup.RESTRICTED: _OpenCodePolicyMapping(),
    AgentToolPolicyGroup.PARTIAL: _OpenCodePolicyMapping(),
    AgentToolPolicyGroup.FULL: _OpenCodePolicyMapping(),
}


def _opencode_go_model_ref(model: str) -> str:
    if "/" in model:
        return model
    return f"{_OPENCODE_GO_PROVIDER_ID}/{model}"


def _opencode_go_config_content() -> str:
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                _OPENCODE_GO_PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "OpenCode Go",
                    "options": {
                        "baseURL": _OPENCODE_GO_BASE_URL,
                        "apiKey": "{env:OPENCODE_GO_API_KEY}",
                    },
                    "models": {model: {"name": model} for model in _OPENCODE_GO_MODELS},
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _extract_usage_limit(event: dict[str, object]) -> UsageLimit | None:
    data = _error_data(event)
    if data is None:
        return None
    if data.get("statusCode") != 429:
        return None
    message = data.get("message")
    if not isinstance(message, str):
        return UsageLimit(reset_time=None, raw_message=None)
    reset_time = parse_reset_time(
        message, ResetTimeSyntaxMode.TRY_AGAIN_UTC_REQUIRED_DATE
    )
    raw_message = None if reset_time is not None else message
    return UsageLimit(reset_time=reset_time, raw_message=raw_message)


def _error_data(event: dict[str, object]) -> dict[str, object] | None:
    error = event.get("error")
    if not isinstance(error, dict):
        return None
    data = error.get("data")
    if not isinstance(data, dict):
        return None
    return data


def _extract_error(event: dict[str, object]) -> HardError | TransientError | None:
    data = _error_data(event)
    if data is None:
        return None

    message = data.get("message")
    if not isinstance(message, str) or not message:
        return None

    status = data.get("statusCode")
    if isinstance(status, int):
        if status >= 500:
            return TransientError(status_code=status, raw_message=message)
        if 400 <= status < 500:
            return HardError(status_code=status, raw_message=message)

    if status is None and message.lower().startswith("model not found:"):
        return HardError(status_code=400, raw_message=message)

    if status is None:
        return TransientError(status_code=None, raw_message=message)

    return None


@dataclasses.dataclass
class OpenCodeService:
    api_key: str | None = None
    _exhausted_until: datetime | None = dataclasses.field(default=None, init=False)

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
        del effort
        policy_mapping = _OPENCODE_POLICY_MAPPINGS[tool_policy_group_for(role)]
        parts = ["opencode run", "--format json"]
        if run_kind == RunKind.RESUME and session_uuid:
            parts.append(f"--session {session_uuid}")
        if model:
            parts.append(f"--model {_opencode_go_model_ref(model)}")
        parts.extend(policy_mapping.cli_args)
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
            env["OPENCODE_CONFIG_CONTENT"] = _opencode_go_config_content()
        return env

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState:
        if request.preferred_provider_session_id is not None:
            return ProviderSessionState(
                RunKind.RESUME,
                request.preferred_provider_session_id,
            )
        if not request.has_resumable_provider_state:
            return ProviderSessionState(RunKind.FRESH, None)
        provider_session_id = request.role_session.service_session_id(self.name)
        if provider_session_id is None:
            return ProviderSessionState(RunKind.FRESH, None)

        exact_transcript_match = False
        if request.require_exact_transcript_match:
            state_dir_session_id = load_state_dir_provider_session_id(
                request.provider_state_dir,
                self.name,
            )
            exact_transcript_match = (
                state_dir_session_id == provider_session_id
                and is_exact_resumable_service_session(
                    request.role_session,
                    self.name,
                    provider_session_id=provider_session_id,
                    provider_state_dir=request.provider_state_dir,
                )
            )
        return ProviderSessionState(
            RunKind.RESUME,
            provider_session_id,
            exact_transcript_match=exact_transcript_match,
        )

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        assistant_turns: list[str] = []
        seen_session_id: str | None = None
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            session_id = event.get("sessionID")
            if (
                isinstance(session_id, str)
                and session_id
                and session_id != seen_session_id
                and on_provider_session_id is not None
            ):
                seen_session_id = session_id
                on_provider_session_id(session_id)

            if event.get("type") == "text":
                part = event.get("part")
                if not isinstance(part, dict):
                    continue
                if part.get("type") != "text":
                    continue
                time = part.get("time")
                if not isinstance(time, dict) or time.get("end") is None:
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                stripped = text.strip()
                if not stripped:
                    continue
                assistant_turns.append(stripped)
                yield AssistantTurn(text=stripped)
                continue

            if event.get("type") == "session.status":
                status = event.get("status")
                if (
                    isinstance(status, dict)
                    and status.get("type") == "idle"
                    and assistant_turns
                ):
                    yield Result(text="\n\n".join(assistant_turns))
                return

            if event.get("type") == "error":
                limit = _extract_usage_limit(event)
                if limit is not None:
                    yield limit
                else:
                    classified = _extract_error(event)
                    if classified is not None:
                        yield classified
                return

    def is_available(self, now: datetime | None = None) -> bool:
        if self._exhausted_until is None:
            return True
        now = now or _time_module.now_local()
        return now >= self._exhausted_until

    def next_wake_time(self) -> datetime:
        if self._exhausted_until is None:
            raise RuntimeError(
                "OpenCodeService.next_wake_time called when not exhausted"
            )
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
            return f"{SESSION_DIR_NAME}/{role.value}/{namespace}/opencode/"
        return f"{SESSION_DIR_NAME}/{role.value}/opencode/"

    def is_resumable(self, state_dir: Path) -> bool:
        return (state_dir / "session_id").is_file()

    def valid_models(self) -> frozenset[str]:
        return frozenset(_OPENCODE_GO_MODELS)

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})
