from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pycastle.agents.output_protocol import AgentRole
from pycastle.services import ClaudeService
from pycastle.services.codex_service import CodexService
from pycastle.services.opencode_service import OpenCodeService
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    RecoveredSessionIdPersistence,
    RunSessionPlan,
)
from pycastle.session import RoleSession, RunKind
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
        provider_session_id=None,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )


def test_run_session_plan_uses_selected_codex_service_state_dir_for_rollout_recovery(
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
