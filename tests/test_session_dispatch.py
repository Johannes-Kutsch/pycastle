from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.session_dispatch import (
    PreparedAgentSession,
    SessionDispatchRequest,
    prepare_agent_session,
    record_successful_provider_session_metadata,
)
from pycastle.session import RoleSession, RunKind
from pycastle.session._provider_session_state import (
    ProviderSessionStateRequest,
    prepare_provider_session_state,
)
from pycastle.errors import HardAgentError
from pycastle.services import ClaudeService, CodexService
from pycastle.services.agent_service import AgentService
from pycastle.services.opencode_service import OpenCodeService


@dataclass
class _LegacyStateDirService:
    name: str = "fake"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del namespace
        return f".pycastle-session/{role.value}/{self.name}/"

    def is_resumable(self, state_dir: Path) -> bool:
        return state_dir.is_dir() and any(state_dir.rglob("*"))


@dataclass
class _CustomOpenCodeStateDirService:
    name: str = "opencode"
    relpath: str = "custom/opencode-state/"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return (state_dir / "session_id").is_file()


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


def _request(
    tmp_path: Path,
    *,
    role: AgentRole = AgentRole.IMPLEMENTER,
    service=None,
    namespace: str = "",
    container_workspace: str = "/home/agent/workspace",
) -> SessionDispatchRequest:
    return SessionDispatchRequest(
        mount_path=tmp_path,
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
) -> ProviderSessionStateRequest:
    return ProviderSessionStateRequest(
        worktree=tmp_path,
        role=role,
        session_namespace=namespace,
        service=service or cast(AgentService, _ClaudeFilesystemStandIn()),
    )


def test_prepare_agent_session_returns_prepared_agent_session(tmp_path: Path):
    session = prepare_agent_session(_request(tmp_path))

    assert isinstance(session, PreparedAgentSession)


def test_prepare_provider_session_state_fresh_claude_uses_deterministic_uuid_and_state_path(
    tmp_path: Path,
):
    state = prepare_provider_session_state(_provider_request(tmp_path))

    assert state.run_kind is RunKind.FRESH
    assert (
        state.provider_session_id
        == RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        ).session_uuid()
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
    assert (
        state.provider_session_id
        == RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        ).session_uuid()
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


def test_prepare_agent_session_fresh_claude_returns_run_kind_fresh(tmp_path: Path):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    assert session.run_kind is RunKind.FRESH


def test_prepare_agent_session_fresh_claude_has_uuid_as_provider_session_id(
    tmp_path: Path,
):
    session = prepare_agent_session(_request(tmp_path, service=ClaudeService()))

    expected = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
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
    expected = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
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

    with pytest.raises(HardAgentError) as exc_info:
        prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert exc_info.value.status_code == 401
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
        RoleSession(tmp_path, AgentRole.IMPLEMENTER).service_session_id("codex")
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
        RoleSession(tmp_path, AgentRole.IMPLEMENTER).service_session_id("codex")
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
        RoleSession(tmp_path, AgentRole.IMPLEMENTER).service_session_id("codex") is None
    )


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
    assert (
        session.provider_session_id
        != RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        ).session_uuid()
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
    assert RoleSession(tmp_path, AgentRole.IMPLEMENTER).service_session_id("codex") == (
        "thread-from-rollout"
    )


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
        RoleSession(tmp_path, AgentRole.IMPLEMENTER).service_session_id("codex") is None
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
    assert (
        session.provider_session_id
        == RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        ).session_uuid()
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


def test_prepare_agent_session_fresh_opencode_uses_provider_state_dir_without_writing_session_files(
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
    provider_state_dir = (
        tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    )

    session.prepare_for_run()

    assert session.run_kind is RunKind.FRESH
    assert session.provider_session_id is None
    assert selected_state_dir.is_dir()
    assert (
        session.provider_state_dir_container_path
        == "/home/agent/workspace/.pycastle-session/improve/main/opencode/"
    )
    assert provider_state_dir.is_dir()
    assert list(provider_state_dir.rglob("*")) == []


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
        session.provider_state_dir_container_path
        == "/home/agent/workspace/custom/opencode-state/"
    )


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
    assert role_session.service_session_id("codex") == "thread-sidecar-id"


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

    assert role_session.service_session_metadata_path.exists() is False

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
    role_session.save_service_session_metadata("claude", "thread-existing")
    before = role_session.service_session_metadata_path.read_text(encoding="utf-8")
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    assert session.provider_session_id is None

    session.success_recorder()

    assert role_session.service_session_metadata("claude") == {
        "service": "claude",
        "provider_session_id": "thread-existing",
    }
    assert role_session.service_session_metadata("codex") is None
    assert (
        role_session.service_session_metadata_path.read_text(encoding="utf-8") == before
    )


def test_prepared_success_recorder_preserves_metadata_for_other_services(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    role_session.save_service_session_metadata("claude", "thread-claude")
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
