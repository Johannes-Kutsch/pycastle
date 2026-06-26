from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path

from pycastle.services.agent_service import (
    AssistantTurn,
    CredentialFailure,
    HardError,
    ParsedTurn,
    Result,
    TransientError,
    UsageLimit,
)
from pycastle.runtime_session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
    is_exact_resumable_service_session,
    load_provider_state_session_id,
    select_resumable_provider_session_id,
)

from ..agents.output_protocol import AgentRole
from ..session.resume import provider_state_relpath
from ..session import RunKind
from .credential_pool import CredentialPool
from .flag_profiles import AgentToolPolicyGroup, tool_policy_group_for
from .reset_time_parser import ResetTimeSyntaxMode, parse_reset_time

_OPENCODE_GO_PROVIDER_ID = "opencode-go"
_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
_OPENCODE_SESSION_ID_FILENAME = "session_id"
_OPENCODE_GO_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5.2",
    "glm-5.1",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "minimax-m2.7",
    "minimax-m3",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
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


def _load_opencode_state_dir_session_id(state_dir: Path | None) -> str | None:
    if state_dir is None:
        return None
    return load_provider_state_session_id(state_dir / _OPENCODE_SESSION_ID_FILENAME)


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


def _extract_credential_failure(event: dict[str, object]) -> CredentialFailure | None:
    data = _error_data(event)
    if data is None:
        return None

    status = data.get("statusCode")
    message = data.get("message")
    error = event.get("error")
    error_name = error.get("name") if isinstance(error, dict) else None
    if (
        status == 401
        and isinstance(message, str)
        and message.lower() == "invalid api key"
        and error_name == "AuthenticationError"
    ):
        return CredentialFailure(
            raw_message=message,
            service_name="opencode",
            classification="operator_actionable_agent_credential_failure",
            source_observations=(("json_event.error", message),),
            status_code=401,
        )
    return None


@dataclasses.dataclass
class OpenCodeService:
    _pool: CredentialPool | None = dataclasses.field(default=None, init=False)
    _current_token: str | None = dataclasses.field(default=None, init=False)

    def __init__(
        self, api_key: str | None = None, accounts: list[tuple[str, str]] | None = None
    ):
        if accounts is not None:
            self._pool = CredentialPool(
                accounts,
                empty_error_message="OpenCodeService requires at least one account",
                unavailable_error_message="No available OpenCode accounts",
            )
            self._current_token = accounts[0][1]
            return
        if api_key is not None:
            self._pool = CredentialPool([("account 1", api_key)])
            self._current_token = api_key
            return
        self._pool = None
        self._current_token = None

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
        *,
        tool_policy: AgentToolPolicyGroup | None = None,
    ) -> str:
        del effort
        policy_mapping = _OPENCODE_POLICY_MAPPINGS[
            tool_policy if tool_policy is not None else tool_policy_group_for(role)
        ]
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
        env: dict[str, str] = {"TZ": "UTC"}
        if state_dir_container_path:
            env["OPENCODE_HOME"] = state_dir_container_path

        if token is None and self._pool is not None:
            _, self._current_token = self._pool.pick()
            token = self._current_token
        elif token is not None:
            self._current_token = token

        if token is not None:
            env["OPENCODE_GO_API_KEY"] = token
            env["OPENCODE_CONFIG_CONTENT"] = _opencode_go_config_content()
        return env

    def provider_session_preferences(
        self, request: ProviderSessionPreferencesRequest
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState:
        state_dir_session_id = _load_opencode_state_dir_session_id(
            request.provider_state_dir
        )
        if not request.has_resumable_provider_state or state_dir_session_id is None:
            return ProviderSessionState(
                RunKind.FRESH,
                None,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
            )
        if request.preferred_provider_session_id is not None:
            return ProviderSessionState(
                RunKind.RESUME,
                request.preferred_provider_session_id,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
                use_service_state_dir_for_container=True,
            )

        selection = select_resumable_provider_session_id(
            request.role_session,
            self.name,
            provider_state_dir=request.provider_state_dir,
            has_resumable_provider_state=request.has_resumable_provider_state,
            recover_provider_session_id=_load_opencode_state_dir_session_id,
        )
        provider_session_id = selection.provider_session_id
        if provider_session_id is None:
            return ProviderSessionState(
                RunKind.FRESH,
                None,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
            )

        exact_transcript_match = False
        if request.require_exact_transcript_match:
            exact_transcript_match = is_exact_resumable_service_session(
                request.role_session,
                self.name,
                provider_session_id=provider_session_id,
                provider_state_dir=request.provider_state_dir,
                exact_provider_session_matcher=lambda session_id, provider_state_dir: (
                    _load_opencode_state_dir_session_id(provider_state_dir)
                    == session_id
                ),
            )
        return ProviderSessionState(
            RunKind.RESUME,
            provider_session_id,
            state_dir_relpath=request.state_dir_relpath,
            state_dir_path=request.provider_state_dir,
            exact_transcript_match=exact_transcript_match,
            persist_provider_session_id=selection.persist_provider_session_id,
            use_service_state_dir_for_container=True,
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
                    classified: ParsedTurn | None = _extract_credential_failure(event)
                    if classified is None:
                        classified = _extract_error(event)
                    if classified is not None:
                        yield classified
                return

    def is_available(self, now: datetime | None = None) -> bool:
        if self._pool is None:
            return True
        return self._pool.has_available(now=now)

    def next_wake_time(self) -> datetime:
        if self._pool is None:
            raise RuntimeError("OpenCodeService.next_wake_time called with no pool")
        return self._pool.earliest_wake_time()

    def mark_exhausted(
        self,
        reset_time: datetime | None,
        *,
        _now: datetime | None = None,
    ) -> None:
        if self._pool is None or self._current_token is None:
            return
        self._pool.mark_exhausted(
            self._current_token,
            reset_time,
            now=_now,
        )

    def mark_permanently_exhausted(self) -> str | None:
        if self._pool is None or self._current_token is None:
            return None
        return self._pool.mark_permanently_exhausted(self._current_token)

    def account_names(self) -> list[str]:
        if self._pool is None:
            return []
        return self._pool.names()

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return provider_state_relpath(role, self.name, namespace)

    def is_resumable(self, state_dir: Path) -> bool:
        return (state_dir / "resume.jsonl").is_file() or (
            state_dir / "session_id"
        ).is_file()

    def valid_models(self) -> frozenset[str]:
        return frozenset(_OPENCODE_GO_MODELS)

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})
