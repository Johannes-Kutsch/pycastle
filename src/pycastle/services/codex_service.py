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
from ..session import RoleSession, RunKind
from ..session._provider_session_decision import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
)
from ..session.provider_session_state import recover_state_dir_provider_session_id
from ..session.service_resume_identity import (
    is_exact_resumable_service_session,
    select_resumable_provider_session_id,
)
from ..provider_errors import ProviderErrorObservation
from .agent_service import (
    AssistantTurn,
    CredentialFailure,
    HardError,
    ParsedTurn,
    PromptTokens,
    TransientError,
    UsageLimit,
)
from .provider_session_state import ProviderSessionState, ProviderSessionStateRequest
from ._wake_time import compute_wake_time
from .flag_profiles import AgentToolPolicyGroup, tool_policy_group_for
from .reset_time_parser import ResetTimeSyntaxMode, parse_reset_time

_log = logging.getLogger(__name__)

_USAGE_LIMIT_SUBSTRING = "You've hit your usage limit"

_UNAUTHORIZED_RE = re.compile(
    r"\b(?:401|unauthorized|missing bearer|basic authentication)\b",
    re.IGNORECASE,
)
_GENERIC_AUTH_RE = re.compile(
    r"\b(?:401|unauthorized|invalid_grant|invalid token|missing bearer|basic authentication)\b",
    re.IGNORECASE,
)
_HTTP_STATUS_RE = re.compile(r"\bstatus\s+(?P<status>\d{3})\b", re.IGNORECASE)
_AUTH_LINEAGE_EXHAUSTED_CLASSIFICATION = "codex_auth_lineage_exhausted"


def _is_exact_refresh_token_reused_message(message: str) -> bool:
    exact_markers = (
        "refresh_token_reused",
        "This refresh token has already been used.",
    )
    return all(marker in message for marker in exact_markers)


def _is_refresh_token_already_used_prose(message: str) -> bool:
    lowered = message.lower()
    return (
        "access token could not be refreshed" in lowered
        and "refresh token was already used" in lowered
    )


def _provider_error_observation(
    *,
    raw_provider_text: str,
    source_stream: str,
    status_code: int | None = None,
    provider_code: str | None = None,
    error_name: str | None = None,
) -> ProviderErrorObservation:
    return ProviderErrorObservation(
        service_name="codex",
        raw_provider_text=raw_provider_text,
        source_stream=source_stream,
        status_code=status_code,
        provider_code=provider_code,
        error_name=error_name,
    )


def _classify_error_message(
    message: str,
    *,
    source_stream: str,
) -> CredentialFailure | HardError | TransientError | None:
    if _is_refresh_token_already_used_prose(message):
        return CredentialFailure(
            raw_message=message,
            service_name="codex",
            status_code=401,
            classification=_AUTH_LINEAGE_EXHAUSTED_CLASSIFICATION,
            source_observations=(
                _provider_error_observation(
                    raw_provider_text=message,
                    source_stream=source_stream,
                    status_code=401,
                ),
            ),
        )
    if _GENERIC_AUTH_RE.search(message):
        if _is_exact_refresh_token_reused_message(message):
            return CredentialFailure(
                raw_message=message,
                service_name="codex",
                status_code=401,
                classification=_AUTH_LINEAGE_EXHAUSTED_CLASSIFICATION,
                source_observations=(
                    _provider_error_observation(
                        raw_provider_text=message,
                        source_stream=source_stream,
                        status_code=401,
                        provider_code="refresh_token_reused",
                    ),
                ),
            )
        return HardError(
            status_code=401,
            raw_message=message,
            observations=(
                _provider_error_observation(
                    raw_provider_text=message,
                    source_stream=source_stream,
                    status_code=401,
                ),
            ),
        )

    match = _HTTP_STATUS_RE.search(message)
    if match is None:
        return None

    status = int(match.group("status"))
    if status >= 500:
        return TransientError(
            status_code=status,
            raw_message=message,
            observations=(
                _provider_error_observation(
                    raw_provider_text=message,
                    source_stream=source_stream,
                    status_code=status,
                ),
            ),
        )
    if 400 <= status < 500:
        return HardError(
            status_code=status,
            raw_message=message,
            observations=(
                _provider_error_observation(
                    raw_provider_text=message,
                    source_stream=source_stream,
                    status_code=status,
                ),
            ),
        )
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
        return RoleSession.provider_state_relpath_for(role, self.name, namespace)

    def is_resumable(self, state_dir: Path) -> bool:
        sessions_dir = state_dir / "sessions"
        if not sessions_dir.is_dir():
            return False
        return any(sessions_dir.rglob("rollout-*.jsonl"))

    def provider_session_state(
        self, request: ProviderSessionStateRequest
    ) -> ProviderSessionState:
        auth_seeding_requirement = _codex_auth_seeding_requirement(
            request.provider_state_dir
        )
        auth_seed_action = _codex_auth_seed_action(request.provider_state_dir)
        if request.preferred_provider_session_id is not None:
            return ProviderSessionState(
                RunKind.RESUME,
                request.preferred_provider_session_id,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
                auth_seeding_requirement=auth_seeding_requirement,
                auth_seed_action=auth_seed_action,
            )
        saved_provider_session_id = request.role_session.service_session_id(self.name)
        if saved_provider_session_id is not None:
            exact_transcript_match = False
            if request.require_exact_transcript_match:
                exact_transcript_match = is_exact_resumable_service_session(
                    request.role_session,
                    self.name,
                    provider_session_id=saved_provider_session_id,
                    provider_state_dir=request.provider_state_dir,
                )
            return ProviderSessionState(
                RunKind.RESUME,
                saved_provider_session_id,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
                exact_transcript_match=exact_transcript_match,
                auth_seeding_requirement=auth_seeding_requirement,
                auth_seed_action=auth_seed_action,
            )
        if not request.has_resumable_provider_state:
            return ProviderSessionState(
                RunKind.FRESH,
                None,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
                auth_seeding_requirement=auth_seeding_requirement,
                auth_seed_action=auth_seed_action,
                allow_protocol_reprompt=not request.force_resume,
            )

        selection = select_resumable_provider_session_id(
            request.role_session,
            self.name,
            provider_state_dir=request.provider_state_dir,
            has_resumable_provider_state=request.has_resumable_provider_state,
        )
        provider_session_id = selection.provider_session_id
        persist_provider_session_id = selection.persist_provider_session_id
        if provider_session_id is None:
            provider_session_id = recover_state_dir_provider_session_id(
                request.provider_state_dir,
                self.name,
            )
            if provider_session_id is not None:
                request.role_session.save_service_session_id(
                    self.name,
                    provider_session_id,
                )
                persist_provider_session_id = True
        if provider_session_id is None:
            return ProviderSessionState(
                RunKind.FRESH,
                None,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
                auth_seeding_requirement=auth_seeding_requirement,
                auth_seed_action=auth_seed_action,
                allow_protocol_reprompt=not request.force_resume,
            )

        exact_transcript_match = False
        if request.require_exact_transcript_match:
            exact_transcript_match = is_exact_resumable_service_session(
                request.role_session,
                self.name,
                provider_session_id=provider_session_id,
                provider_state_dir=request.provider_state_dir,
            )

        return ProviderSessionState(
            RunKind.RESUME,
            provider_session_id,
            state_dir_relpath=request.state_dir_relpath,
            state_dir_path=request.provider_state_dir,
            exact_transcript_match=exact_transcript_match,
            persist_provider_session_id=persist_provider_session_id,
            auth_seeding_requirement=auth_seeding_requirement,
            auth_seed_action=auth_seed_action,
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
                    classified = _classify_error_message(
                        message,
                        source_stream="json_event.turn_failed",
                    )
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
                    classified = _classify_error_message(
                        message,
                        source_stream="json_event.error",
                    )
                    if classified is not None:
                        yield classified
                    _log.warning("codex error: %s", message)
                return


def _codex_auth_seeding_requirement(
    provider_state_dir: Path | None,
) -> AuthSeedingRequirement:
    if provider_state_dir is None or (provider_state_dir / "auth.json").exists():
        return AuthSeedingRequirement.NOT_REQUIRED
    return AuthSeedingRequirement.REQUIRED


def _codex_auth_seed_action(
    provider_state_dir: Path | None,
) -> LocalAuthSeedAction | None:
    if _codex_auth_seeding_requirement(provider_state_dir) is (
        AuthSeedingRequirement.NOT_REQUIRED
    ):
        return None
    if provider_state_dir is None:
        return None
    return LocalAuthSeedAction(
        source=Path.home() / ".codex" / "auth.json",
        destination=provider_state_dir / "auth.json",
    )
