from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.session_state import (
    AgentRunSessionStateRequest,
    prepare_agent_run_session_state,
    record_observed_provider_session_id,
)
from pycastle.errors import AgentCredentialFailureError
from pycastle.services import ClaudeService, CodexService, OpenCodeService
from pycastle.session.agent import RunSessionPlan
from pycastle.session import RoleSession, RunKind


def test_prepare_agent_run_session_state_fresh_claude_uses_derived_uuid_and_service_state_dir(
    tmp_path: Path,
):
    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=ClaudeService(),
        )
    )

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
    assert state.provider_state_dir_relpath == ".pycastle-session/implementer/claude/"
    assert state.auth_seed_action is None
    assert state.codex_auth_seed_input is None


def test_prepare_agent_run_session_state_resume_claude_uses_same_derived_uuid(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "improve" / "main" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            session_namespace="main",
            service=ClaudeService(),
        )
    )

    assert state.run_kind is RunKind.RESUME
    assert (
        state.provider_session_id
        == RoleSession(
            tmp_path,
            AgentRole.IMPROVE,
            "main",
        ).session_uuid()
    )
    assert state.service_state_dir_path == state_dir
    assert state.provider_state_dir_relpath == ".pycastle-session/improve/main/claude/"
    assert state.auth_seed_action is None
    assert state.codex_auth_seed_input is None


def test_prepare_agent_run_session_state_empty_role_dir_stays_fresh_for_claude(
    tmp_path: Path,
):
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    role_dir.mkdir(parents=True)

    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=ClaudeService(),
        )
    )

    assert state.run_kind is RunKind.FRESH
    assert (
        state.provider_session_id
        == RoleSession(
            tmp_path,
            AgentRole.IMPLEMENTER,
        ).session_uuid()
    )
    assert state.service_state_dir_path == role_dir / "claude"
    assert state.provider_state_dir_relpath == ".pycastle-session/implementer/claude/"
    assert state.auth_seed_action is None
    assert state.codex_auth_seed_input is None


def test_prepare_agent_run_session_state_resume_codex_uses_persisted_thread_id(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "improve" / "main" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text("{}", encoding="utf-8")
    (sessions_dir / "rollout-001.jsonl").write_text("{}\n", encoding="utf-8")
    (state_dir / "thread_id").write_text("thread-persisted\n", encoding="utf-8")

    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            session_namespace="main",
            service=CodexService(),
        )
    )

    assert state.run_kind is RunKind.RESUME
    assert state.provider_session_id == "thread-persisted"
    assert state.service_state_dir_path == state_dir
    assert state.provider_state_dir_relpath == ".pycastle-session/improve/main/codex/"


def test_prepare_agent_run_session_state_fresh_codex_without_persisted_or_recoverable_thread_id(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text("{}", encoding="utf-8")
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"turn.completed"}\n',
        encoding="utf-8",
    )

    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=CodexService(),
        )
    )

    assert state.run_kind is RunKind.FRESH
    assert state.provider_session_id is None
    assert state.service_state_dir_path == state_dir
    assert state.provider_state_dir_relpath == ".pycastle-session/implementer/codex/"


def test_prepare_agent_run_session_state_missing_codex_host_auth_raises_operator_actionable_credential_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(AgentCredentialFailureError) as exc_info:
        prepare_agent_run_session_state(
            AgentRunSessionStateRequest(
                worktree=tmp_path,
                role=AgentRole.IMPLEMENTER,
                session_namespace="",
                service=CodexService(),
            )
        )

    assert exc_info.value.service_name == "codex"
    assert exc_info.value.status_code == 401
    assert len(exc_info.value.observations) == 1
    observation = exc_info.value.observations[0]
    assert observation.service_name == "codex"
    assert observation.source_stream == "pre-dispatch host check"


def test_prepare_agent_run_session_state_resume_opencode_uses_persisted_session_id(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("sess-persisted\n", encoding="utf-8")

    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            session_namespace="main",
            service=OpenCodeService(),
        )
    )

    assert state.run_kind is RunKind.RESUME
    assert state.provider_session_id == "sess-persisted"
    assert state.service_state_dir_path == state_dir
    assert (
        state.provider_state_dir_relpath == ".pycastle-session/improve/main/opencode/"
    )


def test_prepare_agent_run_session_state_reuses_planned_opencode_session_id_on_resume_retries(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    state_dir.mkdir(parents=True)
    (state_dir / "resume.jsonl").write_text("{}\n", encoding="utf-8")
    (state_dir / "session_id").write_text("sess-persisted\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=OpenCodeService(),
    )

    (state_dir / "session_id").write_text("\n", encoding="utf-8")

    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            session_namespace="main",
            service=OpenCodeService(),
            run_session_plan=plan,
        )
    )

    resumable_run = state.resumable_provider_run_session()

    assert resumable_run.run_kind is RunKind.RESUME
    assert resumable_run.provider_session_id == "sess-persisted"


def test_session_package_public_interface_reports_no_protocol_resume_for_unrecoverable_codex_state(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "28"
    dir_b = state_dir / "sessions" / "2026" / "05" / "29"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (state_dir / "auth.json").write_text("{}", encoding="utf-8")
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-alpha"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-beta"}\n',
        encoding="utf-8",
    )

    from pycastle.session import (
        AgentRunSessionStateRequest as PublicAgentRunSessionStateRequest,
        prepare_agent_run_session_state as public_prepare_agent_run_session_state,
    )

    state = public_prepare_agent_run_session_state(
        PublicAgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            session_namespace="",
            service=CodexService(),
        )
    )

    assert state.run_kind is RunKind.FRESH
    assert state.protocol_reprompt_provider_run_session() is None


def test_record_observed_provider_session_id_writes_exact_codex_thread_id_file(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "improve" / "main" / "codex"
    state_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text("{}", encoding="utf-8")

    state = prepare_agent_run_session_state(
        AgentRunSessionStateRequest(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            session_namespace="main",
            service=CodexService(),
        )
    )

    record_observed_provider_session_id(state, "thread-exact-value")

    assert (state_dir / "thread_id").read_text(encoding="utf-8") == "thread-exact-value"
