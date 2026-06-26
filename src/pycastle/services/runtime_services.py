from __future__ import annotations

import dataclasses
import enum
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .. import _time as _time_module
from ..runtime_session import (
    session_uuid as runtime_session_uuid,
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
    RunKind,
    is_exact_resumable_service_session,
    load_state_dir_provider_session_id,
    provider_state_relpath,
    select_resumable_provider_session_id,
    load_provider_state_session_id,
)
from ._wake_time import compute_wake_time
from .credential_pool import CredentialPool

if TYPE_CHECKING:
    from ..session.agent import AuthSeedingRequirement, LocalAuthSeedAction
else:
    AuthSeedingRequirement = object
    LocalAuthSeedAction = object


class ToolPolicy(enum.Enum):
    RESTRICTED = "restricted"
    PARTIAL = "partial"
    FULL = "full"


class AgentService(Protocol):
    @property
    def name(self) -> str: ...

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]: ...

    def is_available(self, now: datetime | None = None) -> bool: ...

    def next_wake_time(self) -> datetime: ...

    def mark_exhausted(
        self,
        reset_time: datetime | None,
        *,
        _now: datetime | None = None,
    ) -> None: ...

    def state_dir_relpath(self, role, namespace: str = "") -> str | None: ...

    def is_resumable(self, state_dir: Path) -> bool: ...

    def valid_models(self) -> frozenset[str]: ...

    def valid_efforts(self) -> frozenset[str]: ...

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences: ...

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState: ...


def _provider_session_preferences_for_request(
    request: ProviderSessionPreferencesRequest,
) -> ProviderSessionPreferences:
    return ProviderSessionPreferences(
        preferred_provider_session_id=request.preferred_provider_session_id
    )


def _provider_session_id_for_request(
    request: ProviderSessionStateRequest,
) -> str | None:
    return request.preferred_provider_session_id or _provider_session_uuid_for_request(
        request
    )


def _provider_session_uuid_for_request(
    request: ProviderSessionStateRequest,
) -> str | None:
    legacy_session_uuid = getattr(request.role_session, "session_uuid", None)
    if callable(legacy_session_uuid):
        return legacy_session_uuid()

    role_session_path = getattr(request.role_session, "path", None)
    if not isinstance(role_session_path, Path):
        return None
    identity = _role_session_identity_from_path(role_session_path)
    if identity is None:
        return None
    worktree, role_name, namespace = identity
    return runtime_session_uuid(worktree, role_name, namespace)


def _provider_session_state_for_request(
    request: ProviderSessionStateRequest,
) -> ProviderSessionState:
    exact_transcript_match = False
    run_kind = (
        RunKind.RESUME
        if request.force_resume or request.has_resumable_provider_state
        else RunKind.FRESH
    )
    provider_session_id = _provider_session_id_for_request(request)
    if (
        request.require_exact_transcript_match
        and request.has_resumable_provider_state
        and run_kind is RunKind.RESUME
    ):
        exact_transcript_match = is_exact_resumable_service_session(
            request.role_session,
            "claude",
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


@dataclasses.dataclass
class ClaudeService:
    accounts: list[tuple[str, str]] | None = None
    _pool: CredentialPool | None = dataclasses.field(init=False, default=None)
    _current_token: str | None = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.accounts is None:
            return
        self._pool = CredentialPool(
            self.accounts,
            empty_error_message="ClaudeService requires at least one account",
            unavailable_error_message="No available Claude accounts",
        )

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
        self,
        reset_time: datetime | None,
        *,
        _now: datetime | None = None,
    ) -> None:
        if self._pool is not None and self._current_token is not None:
            self._pool.mark_exhausted(
                self._current_token,
                reset_time,
                now=_now,
            )

    def mark_permanently_exhausted(self) -> str | None:
        if self._pool is None or self._current_token is None:
            return None
        return self._pool.mark_permanently_exhausted(self._current_token)

    def state_dir_relpath(self, role, namespace: str = "") -> str | None:
        return provider_state_relpath(
            role,
            self.name,
            namespace,
            session_root=".pycastle-session",
        )

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(
            candidate.is_file() for candidate in state_dir.rglob("*")
        )

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        return _provider_session_preferences_for_request(request)

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        return _provider_session_state_for_request(request)

    def account_names(self) -> list[str]:
        if self._pool is None:
            return []
        return self._pool.names()

    def valid_models(self) -> frozenset[str]:
        return frozenset({"haiku", "sonnet", "opus"})

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high", "xhigh", "max"})

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


@dataclasses.dataclass
class CodexService:
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
        self,
        reset_time: datetime | None,
        *,
        _now: datetime | None = None,
    ) -> None:
        wake, _ = compute_wake_time(reset_time, _now or _time_module.now_local())
        if wake.tzinfo is None:
            wake = wake.replace(tzinfo=timezone.utc)
        self._exhausted_until = wake

    def state_dir_relpath(self, role, namespace: str = "") -> str | None:
        return provider_state_relpath(
            role,
            self.name,
            namespace,
            session_root=".pycastle-session",
        )

    def is_resumable(self, state_dir: Path) -> bool:
        sessions_dir = state_dir / "sessions"
        return sessions_dir.is_dir() and any(sessions_dir.rglob("rollout-*.jsonl"))

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
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

        saved_provider_session_id = _resolved_provider_session_id(
            request.role_session,
            self.name,
        )
        if saved_provider_session_id is not None:
            exact_transcript_match = False
            if request.require_exact_transcript_match:
                exact_transcript_match = is_exact_resumable_service_session(
                    request.role_session,
                    self.name,
                    provider_session_id=saved_provider_session_id,
                    provider_state_dir=request.provider_state_dir,
                    exact_provider_session_matcher=_is_exact_resumable_codex_session,
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
            provider_session_id = _recover_codex_rollout_thread_id(
                request.provider_state_dir
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
                exact_provider_session_matcher=_is_exact_resumable_codex_session,
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

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        del token
        env = {"TZ": "UTC"}
        if state_dir_container_path:
            env["CODEX_HOME"] = state_dir_container_path
        return env


_OPENCODE_GO_PROVIDER_ID = "opencode-go"
_OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
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


@dataclasses.dataclass
class OpenCodeService:
    api_key: str | None = None
    accounts: list[tuple[str, str]] | None = None
    _pool: CredentialPool | None = dataclasses.field(init=False, default=None)
    _current_token: str | None = dataclasses.field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.accounts is not None:
            self._pool = CredentialPool(
                self.accounts,
                empty_error_message="OpenCodeService requires at least one account",
                unavailable_error_message="No available OpenCode accounts",
            )
            self._current_token = self.accounts[0][1]
            return
        if self.api_key is not None:
            self._pool = CredentialPool([("account 1", self.api_key)])
            self._current_token = self.api_key

    @property
    def name(self) -> str:
        return "opencode"

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
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        state_dir_session_id = load_state_dir_provider_session_id(
            request.provider_state_dir,
            self.name,
            session_id_filename="session_id",
        )
        if not request.has_resumable_provider_state or state_dir_session_id is None:
            return ProviderSessionState(
                RunKind.FRESH,
                None,
                state_dir_relpath=request.state_dir_relpath,
                state_dir_path=request.provider_state_dir,
            )

        provider_session_id = request.preferred_provider_session_id
        exact_transcript_match = False
        if provider_session_id is None:
            selection = select_resumable_provider_session_id(
                request.role_session,
                self.name,
                provider_state_dir=request.provider_state_dir,
                has_resumable_provider_state=request.has_resumable_provider_state,
            )
            provider_session_id = selection.provider_session_id
        if provider_session_id is None:
            provider_session_id = state_dir_session_id

        if request.require_exact_transcript_match and provider_session_id is not None:
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
            use_service_state_dir_for_container=True,
        )

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
        if self._pool is not None and self._current_token is not None:
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

    def state_dir_relpath(self, role, namespace: str = "") -> str | None:
        return provider_state_relpath(
            role,
            self.name,
            namespace,
            session_root=".pycastle-session",
        )

    def is_resumable(self, state_dir: Path) -> bool:
        return (state_dir / "resume.jsonl").is_file() or (
            state_dir / "session_id"
        ).is_file()

    def valid_models(self) -> frozenset[str]:
        return frozenset(_OPENCODE_GO_MODELS)

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"medium"})


def _resolved_provider_session_id(
    role_session: object,
    service_name: str,
) -> str | None:
    legacy_service_session_id = getattr(role_session, "service_session_id", None)
    if callable(legacy_service_session_id):
        saved_provider_session_id = legacy_service_session_id(service_name)
        if saved_provider_session_id is not None:
            return saved_provider_session_id

    role_session_path = getattr(role_session, "path", None)
    if not isinstance(role_session_path, Path):
        return None
    return load_provider_state_session_id(
        _service_session_id_path(role_session_path, service_name)
    )


def _role_session_identity_from_path(
    role_session_path: Path,
) -> tuple[Path, str, str] | None:
    path = role_session_path.resolve()
    parts = path.parts
    try:
        session_root_index = (
            len(parts) - 1 - tuple(reversed(parts)).index(".pycastle-session")
        )
    except ValueError:
        return None
    role_index = session_root_index + 1
    if role_index >= len(parts):
        return None
    role_name = parts[role_index]
    namespace = parts[role_index + 1] if role_index + 1 < len(parts) else ""
    worktree = Path(*parts[:session_root_index])
    return worktree, role_name, namespace


def _service_session_id_path(role_session_path: Path, service_name: str) -> Path:
    filename = "session_id" if service_name == "opencode" else "thread_id"
    return role_session_path / service_name / filename


def _recover_codex_rollout_thread_id(state_dir: Path | None) -> str | None:
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


def _is_exact_resumable_codex_session(
    provider_session_id: str | None,
    provider_state_dir: Path | None,
) -> bool:
    return _recover_codex_rollout_thread_id(provider_state_dir) == provider_session_id


def _codex_auth_seeding_requirement(
    provider_state_dir: Path | None,
) -> AuthSeedingRequirement:
    from ..session.agent import AuthSeedingRequirement as RuntimeAuthSeedingRequirement

    if provider_state_dir is None or (provider_state_dir / "auth.json").exists():
        return RuntimeAuthSeedingRequirement.NOT_REQUIRED
    return RuntimeAuthSeedingRequirement.REQUIRED


def _codex_auth_seed_action(
    provider_state_dir: Path | None,
) -> LocalAuthSeedAction | None:
    from ..session.agent import (
        AuthSeedingRequirement as RuntimeAuthSeedingRequirement,
        LocalAuthSeedAction as RuntimeLocalAuthSeedAction,
    )

    if (
        _codex_auth_seeding_requirement(provider_state_dir)
        is RuntimeAuthSeedingRequirement.NOT_REQUIRED
    ):
        return None
    if provider_state_dir is None:
        return None
    return RuntimeLocalAuthSeedAction(
        source=Path.home() / ".codex" / "auth.json",
        destination=provider_state_dir / "auth.json",
        missing_source_message="Codex authentication missing: run `codex login` on the host.",
        missing_source_service_name="codex",
        missing_source_status_code=401,
    )


__all__ = [
    "AgentService",
    "ClaudeService",
    "CodexService",
    "OpenCodeService",
    "ToolPolicy",
]
