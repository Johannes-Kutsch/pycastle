from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.errors import HardAgentError
from pycastle.services import ClaudeService
from pycastle.services.codex_service import CodexService
from pycastle.services.opencode_service import OpenCodeService
from pycastle.session.agent import RunSessionPlanRequest, plan_run_session
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RecoveredSessionIdPersistence,
    RunSessionPlan,
)
from pycastle.session import RoleSession, RunKind
from pycastle.session._provider_session_sidecars import service_session_id_path
from pycastle.services.agent_service import AgentService


@dataclass
class _FakeAgentService:
    relpath: str | None
    name: str = "fake"
    resumable: bool = False

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return self.resumable


def test_run_session_plan_uses_service_state_dir_for_namespaced_role(tmp_path: Path):
    service = cast(
        AgentService, _FakeAgentService(".pycastle-session/improve/main/codex/")
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan == RunSessionPlan(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
        run_kind=RunKind.FRESH,
        service_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_state_dir_relpath=".pycastle-session/improve/main/codex/",
        host_provider_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_session_id=None,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )


def test_plan_run_session_public_interface_preserves_namespaced_role_session_facts(
    tmp_path: Path,
):
    service = cast(
        AgentService, _FakeAgentService(".pycastle-session/improve/main/codex/")
    )

    plan = plan_run_session(
        RunSessionPlanRequest(
            role=AgentRole.IMPROVE,
            worktree=tmp_path,
            namespace="main",
            service=service,
        )
    )

    assert plan == RunSessionPlan(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
        run_kind=RunKind.FRESH,
        service_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_state_dir_relpath=".pycastle-session/improve/main/codex/",
        host_provider_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_session_id=None,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )


def test_run_session_plan_uses_selected_codex_service_state_dir_for_rollout_recovery_and_persists_sidecar(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )
    state_dir = tmp_path / "custom" / "codex-state"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-custom-state"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.service_state_dir == state_dir
    assert plan.provider_session_id == "thread-from-custom-state"
    assert (
        plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.PERSIST
    )
    assert (
        RoleSession(tmp_path, AgentRole.IMPLEMENTER).service_session_id("codex")
        == "thread-from-custom-state"
    )


def test_run_session_plan_preserves_codex_provider_state_dir_layout_when_service_state_dir_is_custom(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.service_state_dir == tmp_path / "custom" / "codex-state"
    assert plan.provider_state_dir_relpath == ".pycastle-session/implementer/codex/"
    assert plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "implementer" / "codex"
    )


def test_run_session_plan_recovers_namespaced_codex_rollout_from_custom_state_dir_while_preserving_provider_layout(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )
    state_dir = tmp_path / "custom" / "codex-state"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-custom-state"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.service_state_dir == state_dir
    assert plan.provider_state_dir_relpath == ".pycastle-session/improve/main/codex/"
    assert plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "main" / "codex"
    )
    assert plan.provider_session_id == "thread-from-custom-state"
    assert (
        RoleSession(tmp_path, AgentRole.IMPROVE, "main").service_session_id("codex")
        == "thread-from-custom-state"
    )


def test_run_session_plan_recovers_opencode_session_id_from_selected_service_state_dir_while_preserving_provider_layout(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/opencode-state", name="opencode", resumable=True),
    )
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text(
        "sess-from-custom-state",
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.service_state_dir == state_dir
    assert plan.provider_state_dir_relpath == ".pycastle-session/improve/main/opencode/"
    assert plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    )
    assert plan.provider_session_id == "sess-from-custom-state"
    assert (
        RoleSession(tmp_path, AgentRole.IMPROVE, "main").service_session_id("opencode")
        == "sess-from-custom-state"
    )


def test_run_session_plan_reports_fresh_for_selected_opencode_service_state_without_session_id_while_preserving_provider_layout(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/opencode-state", name="opencode", resumable=True),
    )
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.service_state_dir == state_dir
    assert plan.provider_state_dir_relpath == ".pycastle-session/improve/main/opencode/"
    assert plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    )
    assert plan.provider_session_id is None
    assert (
        RoleSession(tmp_path, AgentRole.IMPROVE, "main").service_session_id("opencode")
        is None
    )


def test_run_session_plan_reports_fresh_for_selected_opencode_service_state_with_whitespace_only_session_id(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/opencode-state", name="opencode", resumable=True),
    )
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("   \n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None
    assert (
        RoleSession(tmp_path, AgentRole.IMPROVE, "main").service_session_id("opencode")
        is None
    )


def test_run_session_plan_uses_selected_opencode_service_state_dir_for_resume_container_path(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/opencode-state", name="opencode", resumable=True),
    )
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("sess-from-custom-state", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_state_dir_container_path("/home/agent/workspace") == (
        "/home/agent/workspace/custom/opencode-state/"
    )


def test_run_session_plan_uses_preserved_opencode_provider_layout_for_fresh_container_path(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/opencode-state", name="opencode", resumable=True),
    )
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_state_dir_container_path("/home/agent/workspace") == (
        "/home/agent/workspace/.pycastle-session/implementer/opencode/"
    )


def test_run_session_plan_keeps_none_service_state_dir_when_service_has_no_state_dir(
    tmp_path: Path,
):
    service = cast(AgentService, _FakeAgentService(None))

    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.service_state_dir is None
    assert plan.provider_session_id is None
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.SKIP


def test_run_session_plan_reports_fresh_for_claude_with_absent_or_empty_state_dir(
    tmp_path: Path,
):
    service = ClaudeService()
    expected_relpath = ".pycastle-session/implementer/claude/"
    expected_state_dir = tmp_path / ".pycastle-session/implementer/claude"
    expected_session_id = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()

    absent_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    expected_state_dir.mkdir(parents=True)

    empty_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert absent_plan.run_kind is RunKind.FRESH
    assert empty_plan.run_kind is RunKind.FRESH
    assert absent_plan.provider_state_dir_relpath == expected_relpath
    assert empty_plan.provider_state_dir_relpath == expected_relpath
    assert absent_plan.host_provider_state_dir == expected_state_dir
    assert empty_plan.host_provider_state_dir == expected_state_dir
    assert absent_plan.service_state_dir == expected_state_dir
    assert empty_plan.service_state_dir == expected_state_dir
    assert absent_plan.provider_session_id == expected_session_id
    assert empty_plan.provider_session_id == expected_session_id
    assert absent_plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert empty_plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED


def test_run_session_plan_reports_resume_for_claude_with_populated_state_dir(
    tmp_path: Path,
):
    service = ClaudeService()
    expected_state_dir = tmp_path / ".pycastle-session/implementer/claude"
    expected_session_id = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
    expected_state_dir.mkdir(parents=True)
    (expected_state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.service_state_dir == expected_state_dir
    assert plan.provider_session_id == expected_session_id


def test_run_session_plan_reports_fresh_for_claude_with_metadata_only_role_dir(
    tmp_path: Path,
):
    service = ClaudeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    expected_state_dir = tmp_path / ".pycastle-session/implementer/claude"
    expected_session_id = role_session.session_uuid()
    role_session.save_service_session_metadata("claude", expected_session_id)

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert role_session.is_done() is True
    assert role_session.is_resumable() is False
    assert plan.run_kind is RunKind.FRESH
    assert plan.service_state_dir == expected_state_dir
    assert plan.provider_session_id == expected_session_id
    assert plan.exact_transcript_match is False


def test_run_session_plan_reports_exact_transcript_match_for_claude_only_with_matching_metadata_and_resumable_state(
    tmp_path: Path,
):
    service = ClaudeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    expected_session_id = role_session.session_uuid()

    role_session.save_service_session_metadata("claude", expected_session_id)
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.provider_session_id == expected_session_id
    assert plan.exact_transcript_match is True


def test_run_session_plan_reports_no_exact_transcript_match_for_claude_without_metadata(
    tmp_path: Path,
):
    service = ClaudeService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.exact_transcript_match is False


def test_run_session_plan_reports_no_exact_transcript_match_for_claude_with_mismatched_metadata(
    tmp_path: Path,
):
    service = ClaudeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"

    role_session.save_service_session_metadata("claude", "stale-session-id")
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.exact_transcript_match is False


def test_run_session_plan_reports_no_exact_transcript_match_for_codex_with_conflicting_rollout_thread_ids(
    tmp_path: Path,
):
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "28"
    dir_b = state_dir / "sessions" / "2026" / "05" / "29"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-id-old"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-id-new"}\n',
        encoding="utf-8",
    )
    role_session.save_service_session_metadata("codex", "thread-id-new")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.exact_transcript_match is False


def test_run_session_plan_reports_exact_transcript_match_for_codex_with_matching_metadata_and_unique_rollout(
    tmp_path: Path,
):
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    role_session.save_service_session_id("codex", "thread-abc")
    role_session.save_service_session_metadata("codex", "thread-abc")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-abc"
    assert plan.exact_transcript_match is True


def test_run_session_plan_reports_exact_transcript_match_for_opencode_with_matching_metadata_and_resumable_state(
    tmp_path: Path,
):
    service = OpenCodeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.save_service_session_id("opencode", "sess-opencode-123")
    role_session.save_service_session_metadata("opencode", "sess-opencode-123")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "sess-opencode-123"
    assert plan.exact_transcript_match is True


def test_run_session_plan_reports_no_exact_transcript_match_for_cross_service_metadata(
    tmp_path: Path,
):
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    role_session.save_service_session_metadata("claude", "some-claude-session-id")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.exact_transcript_match is False


def test_run_session_plan_namespaces_claude_provider_session_identity_only_when_non_empty(
    tmp_path: Path,
):
    service = ClaudeService()

    main_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    issues_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="issues",
        service=service,
    )
    base_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert main_plan.provider_session_id != issues_plan.provider_session_id
    assert main_plan.service_state_dir != issues_plan.service_state_dir
    assert (
        base_plan.provider_session_id
        == RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
    )
    assert (
        base_plan.provider_session_id
        == RoleSession(tmp_path, AgentRole.IMPLEMENTER, "").session_uuid()
    )


def test_run_session_plan_uses_namespaced_claude_provider_state_dir_paths(
    tmp_path: Path,
):
    service = ClaudeService()

    main_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    issues_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="issues",
        service=service,
    )

    assert (
        main_plan.provider_state_dir_relpath == ".pycastle-session/improve/main/claude/"
    )
    assert main_plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "main" / "claude"
    )
    assert main_plan.provider_session_id == (
        RoleSession(tmp_path, AgentRole.IMPROVE, "main").session_uuid()
    )
    assert issues_plan.provider_state_dir_relpath == (
        ".pycastle-session/improve/issues/claude/"
    )
    assert issues_plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "issues" / "claude"
    )
    assert issues_plan.provider_session_id == (
        RoleSession(tmp_path, AgentRole.IMPROVE, "issues").session_uuid()
    )


def test_run_session_plan_prefers_saved_codex_thread_id_for_resume(tmp_path: Path):
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    role_session.save_service_session_id("codex", "thread-from-sidecar")
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-from-sidecar"


def test_run_session_plan_recovers_codex_rollout_when_saved_thread_id_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    sessions_dir.mkdir(parents=True)
    role_session.save_service_session_id("codex", "thread-from-sidecar")
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )

    sidecar_path = service_session_id_path(role_session.path, "codex")
    original_read_text = type(sidecar_path).read_text

    def unreadable_read_text(path: Path, *args, **kwargs) -> str:
        if path == sidecar_path:
            raise OSError("thread_id unreadable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(sidecar_path), "read_text", unreadable_read_text)

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-from-rollout"
    assert (
        plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.PERSIST
    )
    assert (
        original_read_text(sidecar_path, encoding="utf-8").strip()
        == "thread-from-rollout"
    )


def test_run_session_plan_recovers_single_codex_rollout_and_marks_it_for_persistence(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-from-rollout"
    assert (
        plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.PERSIST
    )


def test_run_session_plan_recovers_single_codex_rollout_amid_malformed_rollout_noise(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                "{not-json",
                '["not-an-object"]',
                '{"type":"turn.completed","thread_id":"ignored"}',
                '{"type":"thread.started","thread_id":"   "}',
                '{"type":"thread.started","thread_id":"thread-from-rollout"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (sessions_dir / "rollout-002.jsonl").write_bytes(b"\xff\xfe\x00")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-from-rollout"
    assert (
        plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.PERSIST
    )


def test_run_session_plan_treats_duplicate_codex_rollout_thread_id_as_unambiguous(
    tmp_path: Path,
):
    service = CodexService()
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

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-same-id"


def test_run_session_plan_treats_distinct_codex_rollout_thread_ids_as_fresh(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "28"
    dir_b = state_dir / "sessions" / "2026" / "05" / "29"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-id-old"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-id-new"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None


def test_run_session_plan_treats_malformed_only_codex_rollout_state_as_fresh(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "05" / "29"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                "{not-json",
                '["not-an-object"]',
                '{"type":"turn.completed","thread_id":"ignored"}',
                '{"type":"thread.started","thread_id":"   "}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None


def test_run_session_plan_never_generates_pycastle_uuid_for_fresh_codex(
    tmp_path: Path,
):
    service = CodexService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None


def test_run_session_plan_reads_namespaced_opencode_session_id_for_resume(
    tmp_path: Path,
):
    service = OpenCodeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    role_session.save_service_session_id("opencode", "sess-from-sidecar")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "sess-from-sidecar"
    assert plan.service_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    )


def test_run_session_plan_fresh_opencode_when_sidecar_session_id_is_missing(
    tmp_path: Path,
):
    service = OpenCodeService()
    state_dir = tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None


def test_run_session_plan_never_generates_pycastle_uuid_for_fresh_opencode(
    tmp_path: Path,
):
    service = OpenCodeService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None


def test_run_session_plan_requires_auth_seeding_for_fresh_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED


def test_run_session_plan_exposes_auth_seed_action_for_fresh_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    action = plan.auth_seed_action

    assert action is not None
    assert action.source == Path.home() / ".codex" / "auth.json"
    assert action.destination == (
        tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    )


def test_run_session_plan_skips_auth_seed_action_for_fresh_codex_with_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    state_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.auth_seed_action is None


@pytest.mark.parametrize(
    ("service", "role", "namespace", "expected_session_id"),
    [
        (ClaudeService(), AgentRole.PLANNER, "", "claude-session-id"),
        (CodexService(), AgentRole.IMPLEMENTER, "", "thread-codex-123"),
        (OpenCodeService(), AgentRole.IMPROVE, "main", "sess-opencode-123"),
    ],
)
def test_run_session_plan_records_service_session_metadata_on_success(
    tmp_path: Path,
    service: AgentService,
    role: AgentRole,
    namespace: str,
    expected_session_id: str,
):
    plan = RunSessionPlan.for_service(
        role=role,
        worktree=tmp_path,
        namespace=namespace,
        service=service,
    )

    plan.record_successful_run(expected_session_id)

    assert RoleSession(tmp_path, role, namespace).service_session_metadata(
        service.name
    ) == {
        "service": service.name,
        "provider_session_id": expected_session_id,
    }


def test_run_session_plan_captures_codex_provider_session_id_for_same_plan_reuse(
    tmp_path: Path,
):
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=CodexService(),
    )

    plan.capture_provider_session_id("thread-codex-456")
    plan.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert plan.provider_session_id == "thread-codex-456"
    assert role_session.service_session_id("codex") == "thread-codex-456"
    assert role_session.service_session_metadata("codex") == {
        "service": "codex",
        "provider_session_id": "thread-codex-456",
    }


def test_run_session_plan_captures_opencode_provider_session_id_for_same_plan_reuse(
    tmp_path: Path,
):
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=OpenCodeService(),
    )

    plan.capture_provider_session_id("sess-opencode-456")
    plan.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    assert plan.provider_session_id == "sess-opencode-456"
    assert role_session.service_session_id("opencode") == "sess-opencode-456"
    assert role_session.service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-opencode-456",
    }


def test_run_session_plan_requires_auth_seeding_for_resume_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED


def test_run_session_plan_exposes_auth_seed_action_for_resume_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    action = plan.auth_seed_action

    assert plan.run_kind is RunKind.RESUME
    assert action is not None
    assert action.source == Path.home() / ".codex" / "auth.json"
    assert action.destination == state_dir / "auth.json"


def test_run_session_plan_does_not_require_auth_seeding_for_resume_codex_with_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED


def test_run_session_plan_skips_auth_seed_action_when_preserved_codex_provider_state_already_has_auth_json(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )
    state_dir = tmp_path / "custom" / "codex-state"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    provider_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    provider_state_dir.mkdir(parents=True)
    (provider_state_dir / "auth.json").write_text(
        '{"mode":"oauth","origin":"provider"}',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.auth_seed_action is None


def test_local_auth_seed_action_applies_only_to_preserved_codex_provider_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    action = plan.auth_seed_action

    assert action is not None

    action.apply()

    provider_auth = (
        tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    )
    assert (
        provider_auth.read_text(encoding="utf-8") == '{"mode":"oauth","origin":"host"}'
    )
    assert not (tmp_path / "custom" / "codex-state").exists()


def test_local_auth_seed_action_does_not_overwrite_existing_provider_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    service = CodexService()
    provider_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    provider_state_dir.mkdir(parents=True)
    provider_auth = provider_state_dir / "auth.json"
    provider_auth.write_text('{"mode":"oauth","origin":"provider"}', encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.auth_seed_action is None

    LocalAuthSeedAction(
        source=host_auth,
        destination=provider_auth,
    ).apply()

    assert (
        provider_auth.read_text(encoding="utf-8")
        == '{"mode":"oauth","origin":"provider"}'
    )


def test_local_auth_seed_action_copies_only_host_auth_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    host_codex_dir = home / ".codex"
    host_codex_dir.mkdir(parents=True)
    host_auth = host_codex_dir / "auth.json"
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    (host_codex_dir / "config.toml").write_text("model = 'gpt-5.5'\n", encoding="utf-8")
    host_sessions_dir = host_codex_dir / "sessions"
    host_sessions_dir.mkdir()
    (host_sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"host-thread"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    destination = tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    LocalAuthSeedAction(source=host_auth, destination=destination).apply()

    assert destination.read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"host"}'
    )
    provider_state_dir = destination.parent
    assert not (provider_state_dir / "config.toml").exists()
    assert not (provider_state_dir / "sessions").exists()


def test_local_auth_seed_action_require_source_raises_hard_agent_error_when_missing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "auth.json"
    action = LocalAuthSeedAction(source=missing, destination=tmp_path / "dest.json")

    with pytest.raises(HardAgentError) as exc_info:
        action.require_source()

    assert exc_info.value.status_code == 401


def test_run_session_plan_capture_without_record_persists_session_id_but_not_metadata(
    tmp_path: Path,
) -> None:
    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=OpenCodeService(),
    )

    plan.capture_provider_session_id("sess-captured")

    role_session = RoleSession(tmp_path, AgentRole.PLANNER)
    assert role_session.service_session_id("opencode") == "sess-captured"
    assert role_session.service_session_metadata("opencode") is None
