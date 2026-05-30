from __future__ import annotations

from pathlib import Path

from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.session_state import (
    AgentRunSessionStateRequest,
    prepare_agent_run_session_state,
)
from pycastle.services import ClaudeService
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
