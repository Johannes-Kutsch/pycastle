from __future__ import annotations

from pathlib import Path


from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.session_dispatch import (
    PreparedAgentSession,
    SessionDispatchRequest,
    prepare_agent_session,
    record_successful_provider_session_metadata,
)
from pycastle.session import RoleSession, RunKind
from pycastle.services import ClaudeService, CodexService
from pycastle.services.opencode_service import OpenCodeService


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


def test_prepare_agent_session_returns_prepared_agent_session(tmp_path: Path):
    session = prepare_agent_session(_request(tmp_path))

    assert isinstance(session, PreparedAgentSession)


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


def test_prepare_agent_session_codex_with_rollout_returns_run_kind_resume(
    tmp_path: Path,
):
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


def test_prepare_agent_session_computes_container_path_from_workspace(tmp_path: Path):
    session = prepare_agent_session(
        _request(tmp_path, service=ClaudeService(), container_workspace="/workspace")
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


def test_remember_provider_session_id_updates_session_id(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    session.remember_provider_session_id("thread-new-id")

    assert session.provider_session_id == "thread-new-id"


def test_remember_provider_session_id_persists_sidecar_for_codex(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))

    session.remember_provider_session_id("thread-sidecar-id")

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


def test_record_successful_provider_session_metadata_uses_updated_session_id(
    tmp_path: Path,
):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))
    session.remember_provider_session_id("thread-runtime-id")

    record_successful_provider_session_metadata(session)

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    metadata = role_session.service_session_metadata("codex")
    assert metadata is not None
    assert metadata["provider_session_id"] == "thread-runtime-id"


def test_prepare_host_provider_state_dir_creates_directory(tmp_path: Path):
    _seed_codex_auth(tmp_path)
    session = prepare_agent_session(_request(tmp_path, service=CodexService()))
    expected_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"

    session.prepare_host_provider_state_dir()

    assert expected_dir.is_dir()
