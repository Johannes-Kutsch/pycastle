from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
    load_provider_state_session_id,
)
from pycastle.session.run_dispatch import (
    PreparedRunSession as PreparedAgentSession,
    RunSessionRequest as SessionDispatchRequest,
    prepare_run_session as prepare_agent_session,
    record_successful_provider_session_metadata,
)
from pycastle.session.service_session_store import (
    load_service_session_id,
    save_service_session_metadata,
    service_session_metadata_path,
    store_for_role_session,
)
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    RecoveredSessionIdPersistence,
)
from pycastle.session import (
    ProviderSessionStateRequest as PreparedProviderSessionStateRequest,
    RoleSession,
    RunKind,
    prepare_provider_session_state,
)
from pycastle.session_planning import (
    ProviderRunStatePlan,
    ProviderRunStatePlanRequest,
    plan_provider_run_state,
)
from pycastle.runtime_session import (
    is_exact_resumable_service_session,
    select_resumable_provider_session_id,
)
from pycastle.errors import HardAgentError
from pycastle.provider_session_adapter import provider_session_adapter_for_service_name
from pycastle.provider_session_adapter import provider_session_adapter_for_service
from pycastle.services import ClaudeService, CodexService
from pycastle.services.runtime_services import AgentService
from pycastle.services.runtime_services import OpenCodeService
from pycastle.session.role import session_uuid_for_role_session_path


def _role_session_session_uuid(role_session: object) -> str:
    role_session_path = getattr(role_session, "path", None)
    if isinstance(role_session_path, Path):
        identity_uuid = session_uuid_for_role_session_path(role_session_path)
        if identity_uuid is not None:
            return identity_uuid
    legacy = getattr(role_session, "session_uuid", None)
    if callable(legacy):
        return legacy()
    raise AssertionError("Unable to derive role session identifier")


def _role_session_service_session_id(
    role_session: object,
    service_name: str,
) -> str | None:
    role_session_path = getattr(role_session, "path", None)
    if isinstance(role_session_path, Path):
        saved_session_id = load_service_session_id(role_session_path, service_name)
        if saved_session_id is not None:
            return saved_session_id
    legacy = getattr(role_session, "service_session_id", None)
    if callable(legacy):
        return legacy(service_name)
    return None


def _load_opencode_state_dir_session_id(state_dir: Path | None) -> str | None:
    if state_dir is None:
        return None
    return load_provider_state_session_id(state_dir / "session_id")


@dataclass
class _LegacyStateDirService:
    name: str = "fake"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del namespace
        return f".pycastle-session/{role.value}/{self.name}/"

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(state_dir.rglob("*"))

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        if self.name == "claude":
            return ProviderSessionPreferences(
                preferred_provider_session_id="claude-session-id"
            )
        return ProviderSessionPreferences()

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        if self.name == "claude":
            return ProviderSessionState(
                RunKind.RESUME
                if request.has_resumable_provider_state
                else RunKind.FRESH,
                _role_session_session_uuid(request.role_session),
            )
        return ProviderSessionState(
            RunKind.RESUME if request.has_resumable_provider_state else RunKind.FRESH,
            None,
        )


@dataclass
class _CustomOpenCodeStateDirService:
    name: str = "opencode"
    relpath: str = "custom/opencode-state/"
    api_key: str = "go-key"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return (state_dir / "session_id").is_file()

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
        if not request.has_resumable_provider_state:
            return ProviderSessionState(RunKind.FRESH, None)
        selection = select_resumable_provider_session_id(
            request.role_session,
            self.name,
            provider_state_dir=request.provider_state_dir,
            has_resumable_provider_state=request.has_resumable_provider_state,
            recover_provider_session_id=_load_opencode_state_dir_session_id,
        )
        if selection.provider_session_id is None:
            return ProviderSessionState(RunKind.FRESH, None)
        exact_transcript_match = False
        if request.require_exact_transcript_match:
            exact_transcript_match = is_exact_resumable_service_session(
                request.role_session,
                self.name,
                provider_session_id=selection.provider_session_id,
                provider_state_dir=request.provider_state_dir,
            )
        return ProviderSessionState(
            RunKind.RESUME,
            selection.provider_session_id,
            exact_transcript_match=exact_transcript_match,
            persist_provider_session_id=selection.persist_provider_session_id,
        )


@dataclass
class _NoRecomputeOpenCodeService(_CustomOpenCodeStateDirService):
    fail_provider_session_state: bool = False

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        if self.fail_provider_session_state:
            raise AssertionError("provider_session_state should not be recomputed")
        return super().provider_session_state(request)


@dataclass
class _CustomCodexStateDirService:
    name: str = "codex"
    relpath: str = "custom/codex-state/"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return CodexService().is_resumable(state_dir)

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
        return CodexService().provider_session_state(request)


@dataclass
class _ClaudeFilesystemStandIn:
    name: str = "claude"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        if namespace:
            return f".pycastle-session/{role.value}/{namespace}/claude/"
        return f".pycastle-session/{role.value}/claude/"

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(
            path.is_file() for path in state_dir.rglob("*")
        )

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        return ProviderSessionPreferences(
            preferred_provider_session_id=_role_session_session_uuid(
                request.role_session
            )
        )

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        provider_session_id = _role_session_session_uuid(request.role_session)
        exact_transcript_match = False
        if request.require_exact_transcript_match:
            exact_transcript_match = is_exact_resumable_service_session(
                request.role_session,
                self.name,
                provider_session_id=provider_session_id,
                provider_state_dir=request.provider_state_dir,
            )
        return ProviderSessionState(
            RunKind.RESUME if request.has_resumable_provider_state else RunKind.FRESH,
            provider_session_id,
            exact_transcript_match=exact_transcript_match,
        )


def _request(
    tmp_path: Path,
    *,
    role: AgentRole = AgentRole.IMPLEMENTER,
    service=None,
    namespace: str = "",
    container_workspace: str = "/home/agent/workspace",
) -> SessionDispatchRequest:
    return SessionDispatchRequest(
        worktree=tmp_path,
        role=role,
        session_namespace=namespace,
        service=service or ClaudeService(),
        container_workspace=container_workspace,
    )


def _provider_request(
    tmp_path: Path,
    *,
    role: AgentRole = AgentRole.IMPLEMENTER,
    namespace: str = "",
    service: AgentService | None = None,
) -> PreparedProviderSessionStateRequest:
    return PreparedProviderSessionStateRequest(
        worktree=tmp_path,
        role=role,
        session_namespace=namespace,
        service=service or cast(AgentService, _ClaudeFilesystemStandIn()),
    )


def test_prepare_agent_session_returns_prepared_agent_session(tmp_path: Path):
    session = prepare_agent_session(_request(tmp_path))

    assert isinstance(session, PreparedAgentSession)


def test_session_package_public_interface_prepares_fresh_claude_run_state(
    tmp_path: Path,
):
    from pycastle.session import (
        ProviderSessionStateRequest as PublicProviderSessionStateRequest,
        prepare_provider_session_state as public_prepare_provider_session_state,
    )

    state = public_prepare_provider_session_state(
        PublicProviderSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=cast(AgentService, _ClaudeFilesystemStandIn()),
        )
    )

    assert state.run_kind is RunKind.FRESH
    assert state.provider_session_id == _role_session_session_uuid(
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        )
    )
    assert state.service_state_dir_path == (
        tmp_path / ".pycastle-session" / "implementer" / "claude"
    )


def test_session_package_public_interface_prepares_resumed_run_session(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    (state_dir / "projects").mkdir(parents=True)
    (state_dir / "projects" / "transcript.jsonl").write_text(
        '{"type":"message"}\n',
        encoding="utf-8",
    )

    from pycastle.session import (
        PreparedRunSession,
        RunSessionRequest,
        prepare_run_session,
    )

    session = prepare_run_session(
        RunSessionRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=ClaudeService(),
            container_workspace="/workspace",
        )
    )

    assert isinstance(session, PreparedRunSession)
    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == _role_session_session_uuid(
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        )
    )
    assert session.provider_state_dir_container_path == (
        "/workspace/.pycastle-session/implementer/claude/"
    )


def test_session_package_public_interface_records_success_metadata_for_runtime_session_id(
    tmp_path: Path,
):
    from pycastle.session import (
        RunSessionRequest,
        prepare_run_session,
        record_successful_provider_session_metadata as record_public_success_metadata,
    )

    session = prepare_run_session(
        RunSessionRequest(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            session_namespace="main",
            service=OpenCodeService(),
            container_workspace="/workspace",
        )
    )

    session.initial_provider_run_session().record_provider_session_id(
        "sess-opencode-runtime"
    )
    record_public_success_metadata(session)

    assert RoleSession(
        tmp_path,
        AgentRole.IMPROVE,
        "main",
    ).service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-opencode-runtime",
    }


def test_prepare_provider_session_state_fresh_claude_uses_deterministic_uuid_and_state_path(
    tmp_path: Path,
):
    state = prepare_provider_session_state(_provider_request(tmp_path))

    assert state.run_kind is RunKind.FRESH
    assert state.provider_session_id == _role_session_session_uuid(
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        )
    )
    assert state.service_state_dir_path == (
        tmp_path / ".pycastle-session" / "implementer" / "claude"
    )


def test_prepare_provider_session_state_resume_claude_uses_same_deterministic_uuid(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    (state_dir / "projects").mkdir(parents=True)
    (state_dir / "projects" / "transcript.jsonl").write_text(
        '{"type":"message"}\n',
        encoding="utf-8",
    )

    state = prepare_provider_session_state(_provider_request(tmp_path))

    assert state.run_kind is RunKind.RESUME
    assert state.provider_session_id == _role_session_session_uuid(
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        )
    )


def test_prepare_provider_session_state_fresh_prepare_for_run_preserves_wipe_before_fresh_layout(
    tmp_path: Path,
):
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    (role_dir / "stale.txt").parent.mkdir(parents=True)
    (role_dir / "stale.txt").write_text("stale", encoding="utf-8")
    (role_dir / "codex" / "thread_id").parent.mkdir(parents=True)
    (role_dir / "codex" / "thread_id").write_text("thread-old", encoding="utf-8")

    state = prepare_provider_session_state(_provider_request(tmp_path))

    state.prepare_for_run()

    assert sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")
    ) == [
        ".pycastle-session",
        ".pycastle-session/implementer",
        ".pycastle-session/implementer/claude",
    ]


def test_prepare_provider_session_state_fresh_codex_without_role_auth_is_hard_error_before_copying_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    provider_auth = (
        tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    )

    with pytest.raises(HardAgentError):
        prepare_provider_session_state(
            _provider_request(tmp_path, service=CodexService())
        )

    assert provider_auth.exists() is False


def test_prepare_provider_session_state_fresh_codex_applies_host_auth_seed_only_during_prepare_for_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True, exist_ok=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    provider_auth = (
        tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    )

    assert state.run_kind is RunKind.FRESH
    assert state.auth_seeding_requirement.name == "REQUIRED"
    assert provider_auth.exists() is False

    state.prepare_for_run()

    assert provider_auth.read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"host"}'
    )


def test_prepare_provider_session_state_fresh_codex_uses_selected_state_dir_for_auth_seeding_and_container_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True, exist_ok=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            service=cast(AgentService, _CustomCodexStateDirService()),
        )
    )

    state.prepare_for_run()

    selected_state_dir = tmp_path / "custom" / "codex-state"
    legacy_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"

    assert state.run_kind is RunKind.FRESH
    assert state.provider_session_id is None
    assert state.provider_state_dir_container_path("/home/agent/workspace") == (
        "/home/agent/workspace/custom/codex-state/"
    )
    assert (selected_state_dir / "auth.json").read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"host"}'
    )
    assert legacy_state_dir.exists() is False


def test_prepare_provider_session_state_fresh_codex_prepare_for_run_wipes_role_session_before_seeding_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True, exist_ok=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    (role_dir / "stale.txt").parent.mkdir(parents=True)
    (role_dir / "stale.txt").write_text("stale", encoding="utf-8")

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    state.prepare_for_run()

    assert (role_dir / "stale.txt").exists() is False
    assert (role_dir / "codex" / "auth.json").read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"host"}'
    )


def test_prepare_provider_session_state_treats_preseeded_codex_auth_json_alone_as_fresh(
    tmp_path: Path,
):
    auth_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    initial_run = state.initial_provider_run_session()
    resumable_run = state.resumable_provider_run_session()

    assert state.run_kind is RunKind.FRESH
    assert state.provider_session_id is None
    assert initial_run.run_kind is RunKind.FRESH
    assert initial_run.provider_session_id is None
    assert resumable_run.run_kind is RunKind.FRESH
    assert resumable_run.provider_session_id is None


def test_prepare_agent_session_fresh_claude_returns_run_kind_fresh(tmp_path: Path):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    assert session.run_kind is RunKind.FRESH


def test_prepare_agent_session_fresh_claude_has_uuid_as_provider_session_id(
    tmp_path: Path,
):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    expected = _role_session_session_uuid(RoleSession(tmp_path, AgentRole.IMPLEMENTER))
    assert session.provider_session_id == expected


def test_prepare_agent_session_fresh_claude_exposes_state_dir_relpath(
    tmp_path: Path,
):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    assert session.run_kind is RunKind.FRESH
    assert session.provider_state_dir_relpath == ".pycastle-session/implementer/claude/"


def test_prepare_agent_session_resume_claude_returns_run_kind_resume(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "projects").mkdir()
    (state_dir / "projects" / "transcript.jsonl").write_text(
        '{"type":"message"}\n', encoding="utf-8"
    )

    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    assert session.run_kind is RunKind.RESUME
    expected = _role_session_session_uuid(RoleSession(tmp_path, AgentRole.IMPLEMENTER))
    assert session.provider_session_id == expected


def _seed_codex_auth(tmp_path: Path) -> None:
    auth_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")


def test_prepare_agent_session_fresh_codex_returns_run_kind_fresh(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.FRESH


def test_prepare_agent_session_fresh_codex_has_no_provider_session_id(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.provider_session_id is None


def test_prepare_agent_session_fresh_codex_missing_host_auth_is_dispatcher_hard_error(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(HardAgentError):
        prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert not (tmp_path / ".pycastle-session" / "implementer").exists()


def test_prepare_agent_session_resume_codex_with_provider_auth_does_not_require_host_auth(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-xyz"}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == "thread-xyz"


def test_prepare_provider_session_state_recovers_single_nested_codex_rollout_thread_id_and_persists_it(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    rollout_dir = (
        tmp_path
        / ".pycastle-session"
        / "implementer"
        / "codex"
        / "sessions"
        / "2026"
        / "05"
        / "29"
        / "nested"
    )
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"   "}',
                '{"type":"thread.started","thread_id":"thread-from-rollout"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    initial_run = state.initial_provider_run_session()

    assert state.run_kind is RunKind.RESUME
    assert state.provider_session_id == "thread-from-rollout"
    assert initial_run.run_kind is RunKind.RESUME
    assert initial_run.provider_session_id == "thread-from-rollout"
    assert (
        _role_session_service_session_id(
            RoleSession(tmp_path, AgentRole.IMPLEMENTER), "codex"
        )
        == "thread-from-rollout"
    )


def test_prepare_provider_session_state_treats_duplicate_nested_codex_rollout_thread_ids_as_unambiguous(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    dir_a = (
        tmp_path
        / ".pycastle-session"
        / "implementer"
        / "codex"
        / "sessions"
        / "2026"
        / "05"
        / "28"
    )
    dir_b = (
        tmp_path
        / ".pycastle-session"
        / "implementer"
        / "codex"
        / "sessions"
        / "2026"
        / "05"
        / "29"
        / "nested"
    )
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-same-id"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-same-id"}\n',
        encoding="utf-8",
    )

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    assert state.run_kind is RunKind.RESUME
    assert state.provider_session_id == "thread-same-id"
    assert (
        _role_session_service_session_id(
            RoleSession(tmp_path, AgentRole.IMPLEMENTER), "codex"
        )
        == "thread-same-id"
    )


def test_prepare_provider_session_state_treats_distinct_nested_codex_rollout_thread_ids_as_fresh(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    dir_a = (
        tmp_path
        / ".pycastle-session"
        / "implementer"
        / "codex"
        / "sessions"
        / "2026"
        / "05"
        / "28"
    )
    dir_b = (
        tmp_path
        / ".pycastle-session"
        / "implementer"
        / "codex"
        / "sessions"
        / "2026"
        / "05"
        / "29"
        / "nested"
    )
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-alpha"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-beta"}\n',
        encoding="utf-8",
    )

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    initial_run = state.initial_provider_run_session()
    resumable_run = state.resumable_provider_run_session()

    assert state.run_kind is RunKind.FRESH
    assert state.provider_session_id is None
    assert initial_run.run_kind is RunKind.FRESH
    assert initial_run.provider_session_id is None
    assert resumable_run.run_kind is RunKind.FRESH
    assert resumable_run.provider_session_id is None
    assert (
        _role_session_service_session_id(
            RoleSession(tmp_path, AgentRole.IMPLEMENTER), "codex"
        )
        is None
    )


def test_prepare_provider_session_state_protocol_reprompt_skips_codex_when_conflicting_rollout_thread_ids_are_unrecoverable(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "28"
    dir_b = state_dir / "sessions" / "2026" / "05" / "29"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-alpha"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-beta"}\n',
        encoding="utf-8",
    )

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    assert state.run_kind is RunKind.FRESH
    assert state.protocol_reprompt_provider_run_session() is None


def test_prepare_provider_session_state_protocol_reprompt_resumes_codex_when_duplicate_rollout_thread_ids_match(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "28"
    dir_b = state_dir / "sessions" / "2026" / "05" / "29"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-same-id"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-same-id"}\n',
        encoding="utf-8",
    )

    state = prepare_provider_session_state(
        _provider_request(tmp_path, service=CodexService())
    )

    reprompt_run = state.protocol_reprompt_provider_run_session()

    assert reprompt_run is not None
    assert reprompt_run.run_kind is RunKind.RESUME
    assert reprompt_run.provider_session_id == "thread-same-id"


def test_prepare_agent_session_prefers_persisted_codex_thread_id_for_resume(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "30"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")
    RoleSession(tmp_path, AgentRole.IMPLEMENTER).save_service_session_id(
        "codex",
        "thread-from-sidecar",
    )

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == "thread-from-sidecar"


def test_prepare_agent_session_resumes_codex_from_saved_thread_id_without_sessions_dir(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    state_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")
    RoleSession(tmp_path, AgentRole.IMPLEMENTER).save_service_session_id(
        "codex",
        "thread-from-sidecar",
    )

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == "thread-from-sidecar"


def test_prepare_agent_session_codex_resume_never_uses_role_session_uuid_as_provider_session_id(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "30"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")
    RoleSession(tmp_path, AgentRole.IMPLEMENTER).save_service_session_id(
        "codex",
        "thread-from-sidecar",
    )

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == "thread-from-sidecar"
    assert session.provider_session_id != _role_session_session_uuid(
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        )
    )


def test_prepare_agent_session_persists_recovered_codex_thread_id_for_future_resume(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "30"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == "thread-from-rollout"
    assert _role_session_service_session_id(
        RoleSession(tmp_path, AgentRole.IMPLEMENTER), "codex"
    ) == ("thread-from-rollout")


def test_prepare_agent_session_falls_back_to_fresh_for_codex_with_distinct_rollout_thread_ids_without_writing_sidecar(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "29"
    dir_b = state_dir / "sessions" / "2026" / "05" / "30"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-old"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-new"}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.FRESH
    assert session.provider_session_id is None
    assert (
        _role_session_service_session_id(
            RoleSession(tmp_path, AgentRole.IMPLEMENTER), "codex"
        )
        is None
    )


def test_prepare_agent_session_falls_back_to_fresh_for_codex_without_thread_started_rollouts_without_writing_sidecar(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "30"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}',
                '{"type":"thread.started","thread_id":"   "}',
                '{"type":"not-thread-started","thread_id":"thread-ignored"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.run_kind is RunKind.FRESH
    assert session.provider_session_id is None
    assert (
        _role_session_service_session_id(
            RoleSession(tmp_path, AgentRole.IMPLEMENTER), "codex"
        )
        is None
    )


def test_prepare_agent_session_start_fresh_preserves_existing_codex_auth_json(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "30"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n',
        encoding="utf-8",
    )
    auth_path = state_dir / "auth.json"
    auth_path.write_text(
        '{"mode":"oauth","origin":"provider"}',
        encoding="utf-8",
    )

    session = prepare_agent_session(_request(tmp_path, service=CodexService()))
    session.prepare_for_run()

    assert session.run_kind is RunKind.FRESH
    assert session.provider_session_id is None
    assert auth_path.read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"provider"}'
    )


def test_prepare_agent_session_namespaced_role_uses_namespace_in_session_id(
    tmp_path: Path,
):
    session_main = prepare_agent_session(
        _request(tmp_path, role=AgentRole.IMPROVE, namespace="main")
    )
    session_issues = prepare_agent_session(
        _request(tmp_path, role=AgentRole.IMPROVE, namespace="issues")
    )

    assert session_main.provider_session_id != session_issues.provider_session_id


def test_prepare_agent_session_improve_main_uses_namespaced_provider_state_dir_for_legacy_service_relpath(
    tmp_path: Path,
):
    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=cast(AgentService, _LegacyStateDirService()),
            container_workspace="/workspace",
        )
    )

    assert session.provider_state_dir_relpath == ".pycastle-session/improve/main/fake/"
    assert session.provider_state_dir_container_path == (
        "/workspace/.pycastle-session/improve/main/fake/"
    )


def test_prepare_agent_session_namespaced_resume_state_does_not_leak_between_namespaces_for_legacy_service_relpath(
    tmp_path: Path,
):
    legacy_state_dir = tmp_path / ".pycastle-session" / "improve" / "claude"
    legacy_state_dir.mkdir(parents=True)
    (legacy_state_dir / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            namespace="issues",
            service=cast(AgentService, _LegacyStateDirService(name="claude")),
        )
    )

    assert session.run_kind is RunKind.FRESH


def test_prepare_agent_session_improve_issues_uses_namespaced_provider_state_dir_for_legacy_service_relpath(
    tmp_path: Path,
):
    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            namespace="issues",
            service=cast(AgentService, _LegacyStateDirService()),
            container_workspace="/workspace",
        )
    )

    assert session.provider_state_dir_relpath == (
        ".pycastle-session/improve/issues/fake/"
    )
    assert session.provider_state_dir_container_path == (
        "/workspace/.pycastle-session/improve/issues/fake/"
    )


def test_prepare_agent_session_empty_namespace_preserves_legacy_path_and_uuid_for_legacy_service_relpath(
    tmp_path: Path,
):
    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=cast(AgentService, _LegacyStateDirService(name="claude")),
        )
    )

    assert session.provider_state_dir_relpath == ".pycastle-session/implementer/claude/"
    assert session.provider_session_id == _role_session_session_uuid(
        RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        )
    )


def test_prepare_agent_session_computes_container_path_from_workspace(tmp_path: Path):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    assert session.provider_state_dir_container_path is not None
    assert session.provider_state_dir_container_path.startswith(
        "/home/agent/workspace/"
    )


def test_prepare_agent_session_preserves_requested_container_workspace(
    tmp_path: Path,
):
    session = prepare_agent_session(
        _request(
            tmp_path,
            service=ClaudeService(),
            container_workspace="/workspace",
        )
    )

    assert session.provider_state_dir_container_path is not None
    assert session.provider_state_dir_container_path.startswith("/workspace/")


def test_prepare_agent_session_no_state_dir_service_yields_none_container_path(
    tmp_path: Path,
):
    session = prepare_agent_session(
        _request(tmp_path, role=AgentRole.PLANNER, service=ClaudeService())
    )

    assert session.provider_state_dir_container_path is not None


def test_prepare_agent_session_opencode_fresh_has_no_provider_session_id(
    tmp_path: Path,
):
    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=OpenCodeService(),
            namespace="main",
        )
    )

    assert session.run_kind is RunKind.FRESH
    assert session.provider_session_id is None


def test_prepare_agent_session_fresh_opencode_uses_selected_provider_state_dir_without_writing_session_files(
    tmp_path: Path,
):
    selected_state_dir = tmp_path / "custom" / "opencode-state"
    selected_state_dir.mkdir(parents=True)

    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=cast(AgentService, _CustomOpenCodeStateDirService()),
            namespace="main",
        )
    )
    session.prepare_for_run()

    assert session.run_kind is RunKind.FRESH
    assert session.provider_session_id is None
    assert selected_state_dir.is_dir()
    assert (
        session.provider_state_dir_container_path
        == "/home/agent/workspace/custom/opencode-state/"
    )


def test_prepare_agent_session_opencode_with_saved_session_resumes(tmp_path: Path):
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.save_service_session_id("opencode", "sess-opencode-resume")

    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=OpenCodeService(),
            namespace="main",
        )
    )

    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == "sess-opencode-resume"


def test_prepare_provider_session_state_resume_opencode_uses_persisted_session_id_for_dispatch(
    tmp_path: Path,
):
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)

    initial_state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=cast(AgentService, _CustomOpenCodeStateDirService()),
            namespace="main",
        )
    )
    initial_state.record_provider_session_id("sess-opencode-resume")

    resumed_state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=cast(AgentService, _CustomOpenCodeStateDirService()),
            namespace="main",
        )
    )
    initial_run = resumed_state.initial_provider_run_session()

    assert resumed_state.run_kind is RunKind.RESUME
    assert resumed_state.provider_session_id == "sess-opencode-resume"
    assert initial_run.run_kind is RunKind.RESUME
    assert initial_run.provider_session_id == "sess-opencode-resume"


def test_prepare_provider_session_state_uses_supplied_provider_run_state_plan_without_recomputing_resume_state(
    tmp_path: Path,
):
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("sess-planned\n", encoding="utf-8")
    service = _NoRecomputeOpenCodeService()
    request = ProviderRunStatePlanRequest(
        worktree=tmp_path,
        role=AgentRole.IMPROVE,
        namespace="main",
        service=cast(AgentService, service),
        role_session=store_for_role_session(
            RoleSession(tmp_path, AgentRole.IMPROVE, "main")
        ),
        provider_session_adapter=provider_session_adapter_for_service(
            cast(AgentService, service)
        ),
    )
    planned_state = plan_provider_run_state(request)
    (state_dir / "session_id").write_text("\n", encoding="utf-8")
    service.fail_provider_session_state = True

    state = prepare_provider_session_state(
        PreparedProviderSessionStateRequest(
            tmp_path,
            AgentRole.IMPROVE,
            "main",
            cast(AgentService, service),
            provider_run_state_plan=planned_state,
        )
    )
    initial_run = state.initial_provider_run_session()
    resumable_run = state.resumable_provider_run_session()

    assert state.run_kind is RunKind.RESUME
    assert state.provider_session_id == "sess-planned"
    assert initial_run.run_kind is RunKind.RESUME
    assert initial_run.provider_session_id == "sess-planned"
    assert resumable_run.run_kind is RunKind.RESUME
    assert resumable_run.provider_session_id == "sess-planned"


def test_prepare_provider_session_state_uses_supplied_provider_run_state_plan_for_opencode_resume_container_path(
    tmp_path: Path,
):
    selected_state_dir = tmp_path / "custom" / "opencode-state"
    role_service_state_dir = (
        tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    )
    service = _NoRecomputeOpenCodeService(fail_provider_session_state=True)
    plan = ProviderRunStatePlan(
        role_session=store_for_role_session(
            RoleSession(tmp_path, AgentRole.IMPROVE, "main")
        ),
        provider_session_adapter=provider_session_adapter_for_service_name(
            service.name
        ),
        service_name=service.name,
        run_kind=RunKind.RESUME,
        provider_session_id="sess-planned",
        provider_state_dir_relpath="custom/opencode-state/",
        provider_state_dir=selected_state_dir,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
        service_state_dir=role_service_state_dir,
        use_service_state_dir_for_container=True,
    )

    state = prepare_provider_session_state(
        PreparedProviderSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            session_namespace="main",
            service=cast(AgentService, service),
            provider_run_state_plan=plan,
        )
    )

    assert state.run_kind is RunKind.RESUME
    assert state.provider_session_id == "sess-planned"
    assert state.service_state_dir_path == selected_state_dir
    assert state.provider_state_dir_container_path("/home/agent/workspace") == (
        "/home/agent/workspace/.pycastle-session/improve/main/opencode/"
    )


def test_prepare_agent_session_opencode_run_session_switches_from_fresh_to_resume_after_capture(
    tmp_path: Path,
):
    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=OpenCodeService(),
            namespace="main",
        )
    )

    initial_run = session.initial_provider_run_session()
    resumable_before_capture = session.resumable_provider_run_session()
    session.on_provider_session_id("sess-opencode-runtime")
    resumable_after_capture = session.resumable_provider_run_session()

    assert initial_run.run_kind is RunKind.FRESH
    assert initial_run.provider_session_id is None
    assert resumable_before_capture.run_kind is RunKind.FRESH
    assert resumable_before_capture.provider_session_id is None
    assert resumable_after_capture.run_kind is RunKind.RESUME
    assert resumable_after_capture.provider_session_id == "sess-opencode-runtime"


def test_prepare_provider_session_state_fresh_opencode_when_selected_state_dir_exists_without_session_id(
    tmp_path: Path,
):
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    (state_dir / "history.jsonl").write_text('{"type":"text"}\n', encoding="utf-8")

    state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=cast(AgentService, _CustomOpenCodeStateDirService()),
            namespace="main",
        )
    )
    initial_run = state.initial_provider_run_session()
    resumable_run = state.resumable_provider_run_session()

    assert state.run_kind is RunKind.FRESH
    assert state.provider_session_id is None
    assert initial_run.run_kind is RunKind.FRESH
    assert initial_run.provider_session_id is None
    assert resumable_run.run_kind is RunKind.FRESH
    assert resumable_run.provider_session_id is None


def test_prepare_agent_session_opencode_resume_uses_selected_service_state_dir(
    tmp_path: Path,
):
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("sess-from-custom-state", encoding="utf-8")

    session = prepare_agent_session(
        _request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=cast(AgentService, _CustomOpenCodeStateDirService()),
            namespace="main",
        )
    )

    assert session.run_kind is RunKind.RESUME
    assert session.provider_session_id == "sess-from-custom-state"
    assert (
        _role_session_service_session_id(
            RoleSession(tmp_path, AgentRole.IMPROVE, "main"), "opencode"
        )
        == "sess-from-custom-state"
    )
    assert (
        session.provider_state_dir_container_path
        == "/home/agent/workspace/custom/opencode-state/"
    )


def test_prepare_provider_session_state_captures_opencode_session_id_in_selected_state_dir_without_api_key_material(
    tmp_path: Path,
):
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    service = _CustomOpenCodeStateDirService()

    state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=cast(AgentService, service),
            namespace="main",
        )
    )

    state.prepare_for_run()
    state.record_provider_session_id("sess-opencode-runtime")
    state.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    session_file_text = {
        path.relative_to(tmp_path).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(
            {
                *state_dir.rglob("*"),
                *(tmp_path / ".pycastle-session" / "improve" / "main").rglob("*"),
            }
        )
        if path.is_file()
    }

    assert (state_dir / "session_id").read_text(encoding="utf-8") == (
        "sess-opencode-runtime"
    )
    assert (
        _role_session_service_session_id(role_session, "opencode")
        == "sess-opencode-runtime"
    )
    assert role_session.service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-opencode-runtime",
    }
    assert all("go-key" not in contents for contents in session_file_text.values())


def test_prepared_provider_run_session_records_success_metadata_with_runtime_session_id(
    tmp_path: Path,
):
    state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=OpenCodeService(),
            namespace="main",
        )
    )

    run_session = state.initial_provider_run_session()
    run_session.record_provider_session_id("sess-opencode-runtime")
    run_session.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    assert role_session.service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-opencode-runtime",
    }
    assert json.loads(
        service_session_metadata_path(role_session.path).read_text(encoding="utf-8")
    ) == {
        "opencode": {
            "service": "opencode",
            "provider_session_id": "sess-opencode-runtime",
        }
    }


def test_prepared_provider_resume_run_session_records_success_metadata_with_latest_runtime_session_id(
    tmp_path: Path,
):
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.save_service_session_id("opencode", "sess-opencode-previous")

    state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=OpenCodeService(),
            namespace="main",
        )
    )

    run_session = state.resumable_provider_run_session()
    run_session.record_provider_session_id("sess-opencode-latest")
    run_session.record_successful_run()

    assert role_session.service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-opencode-latest",
    }


def test_prepared_provider_run_session_capture_without_success_leaves_metadata_absent(
    tmp_path: Path,
):
    state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=OpenCodeService(),
            namespace="main",
        )
    )

    run_session = state.initial_provider_run_session()
    run_session.record_provider_session_id("sess-opencode-runtime")

    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    assert (
        _role_session_service_session_id(role_session, "opencode")
        == "sess-opencode-runtime"
    )
    assert role_session.service_session_metadata("opencode") is None


def test_prepared_provider_run_session_metadata_survives_completion_cleanup(
    tmp_path: Path,
):
    state = prepare_provider_session_state(
        _provider_request(
            tmp_path,
            role=AgentRole.IMPROVE,
            service=OpenCodeService(),
            namespace="main",
        )
    )

    run_session = state.initial_provider_run_session()
    run_session.record_provider_session_id("sess-opencode-runtime")
    run_session.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.clear_provider_state_and_signal_completion()

    assert role_session.service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-opencode-runtime",
    }


def test_remember_provider_session_id_updates_session_id(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    session.on_provider_session_id("thread-new-id")

    assert session.provider_session_id == "thread-new-id"


def test_remember_provider_session_id_persists_sidecar_for_codex(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    session.on_provider_session_id("thread-sidecar-id")

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert (
        _role_session_service_session_id(role_session, "codex") == "thread-sidecar-id"
    )


def test_record_successful_provider_session_metadata_saves_metadata(tmp_path: Path):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    record_successful_provider_session_metadata(session)

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    metadata = role_session.service_session_metadata("claude")
    assert metadata is not None
    assert metadata["service"] == "claude"
    assert metadata["provider_session_id"] == session.provider_session_id


def test_prepare_agent_session_does_not_write_metadata_before_prepared_success_recording(
    tmp_path: Path,
):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)

    assert service_session_metadata_path(role_session.path).exists() is False

    session.success_recorder()

    assert role_session.service_session_metadata("claude") == {
        "service": "claude",
        "provider_session_id": session.provider_session_id,
    }


def test_prepared_success_recorder_without_provider_session_id_leaves_metadata_unchanged(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    save_service_session_metadata(role_session.path, "claude", "thread-existing")
    before = service_session_metadata_path(role_session.path).read_text(
        encoding="utf-8"
    )
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.provider_session_id is None

    session.success_recorder()

    assert role_session.service_session_metadata("claude") == {
        "service": "claude",
        "provider_session_id": "thread-existing",
    }
    assert role_session.service_session_metadata("codex") is None
    assert (
        service_session_metadata_path(role_session.path).read_text(encoding="utf-8")
        == before
    )


def test_prepared_provider_run_session_without_provider_session_id_clears_stale_metadata_for_service(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    save_service_session_metadata(role_session.path, "claude", "thread-claude")
    save_service_session_metadata(role_session.path, "codex", "thread-stale")

    state = prepare_provider_session_state(
        PreparedProviderSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=CodexService(),
        )
    )

    state.initial_provider_run_session().record_successful_run()

    assert role_session.service_session_metadata("codex") is None
    assert role_session.service_session_metadata("claude") == {
        "service": "claude",
        "provider_session_id": "thread-claude",
    }


def test_prepared_provider_run_session_without_provider_session_id_deletes_file_when_sole_service(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    save_service_session_metadata(role_session.path, "codex", "thread-stale")

    state = prepare_provider_session_state(
        PreparedProviderSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=CodexService(),
        )
    )

    state.initial_provider_run_session().record_successful_run()

    assert role_session.service_session_metadata("codex") is None
    assert not service_session_metadata_path(role_session.path).exists()


def test_prepared_success_recorder_preserves_metadata_for_other_services(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    save_service_session_metadata(role_session.path, "claude", "thread-claude")
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))
    session.on_provider_session_id("thread-codex")

    session.success_recorder()

    assert role_session.service_session_metadata("claude") == {
        "service": "claude",
        "provider_session_id": "thread-claude",
    }
    assert role_session.service_session_metadata("codex") == {
        "service": "codex",
        "provider_session_id": "thread-codex",
    }


def test_record_successful_provider_session_metadata_uses_updated_session_id(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))
    session.on_provider_session_id("thread-runtime-id")

    record_successful_provider_session_metadata(session)

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    metadata = role_session.service_session_metadata("codex")
    assert metadata is not None
    assert metadata["provider_session_id"] == "thread-runtime-id"


def test_prepare_host_provider_state_dir_creates_directory(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))
    expected_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"

    session.prepare_for_run()

    assert expected_dir.is_dir()
