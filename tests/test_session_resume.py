"""Tests for session_resume: RoleSession lifecycle and stage/session helpers."""

import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.services.agent_service import AgentService
from pycastle.services.codex_service import CodexService
from pycastle.services.service_registry import ServiceRegistry
from pycastle.session import (
    ProviderFreshFallbackReason,
    ProviderIdentityKind,
    ProviderRunState,
    RoleSession,
    RunKind,
    any_role_dir_present,
    is_stage_done_for,
)
from pycastle.session._provider_session_sidecars import service_session_metadata_path


@dataclass(frozen=True)
class _FakeService:
    name: str
    relpath: str | None
    resumable: bool

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return self.resumable


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def worktree(tmp_path):
    return tmp_path


@pytest.fixture
def rs(worktree):
    return RoleSession(worktree, AgentRole.IMPLEMENTER)


# ── session_uuid determinism ──────────────────────────────────────────────────


def test_session_uuid_is_deterministic(worktree):
    assert (
        RoleSession(worktree, AgentRole.IMPLEMENTER).session_uuid()
        == RoleSession(worktree, AgentRole.IMPLEMENTER).session_uuid()
    )


def test_session_uuid_differs_by_role(worktree):
    assert (
        RoleSession(worktree, AgentRole.IMPLEMENTER).session_uuid()
        != RoleSession(worktree, AgentRole.REVIEWER).session_uuid()
    )


def test_session_uuid_differs_by_worktree(tmp_path):
    a = RoleSession(tmp_path / "issue-1", AgentRole.IMPLEMENTER).session_uuid()
    b = RoleSession(tmp_path / "issue-2", AgentRole.IMPLEMENTER).session_uuid()
    assert a != b


def test_session_uuid_differs_by_namespace(worktree):
    a = RoleSession(worktree, AgentRole.IMPROVE, "main").session_uuid()
    b = RoleSession(worktree, AgentRole.IMPROVE, "issues").session_uuid()
    assert a != b


def test_session_uuid_empty_namespace_equals_no_namespace(worktree):
    assert (
        RoleSession(worktree, AgentRole.IMPLEMENTER).session_uuid()
        == RoleSession(worktree, AgentRole.IMPLEMENTER, "").session_uuid()
    )


def test_session_uuid_resolved_path_equals_direct(worktree):
    assert (
        RoleSession(worktree, AgentRole.IMPLEMENTER).session_uuid()
        == RoleSession(worktree.resolve(), AgentRole.IMPLEMENTER).session_uuid()
    )


def test_session_uuid_is_valid_uuid_string(worktree):
    result = RoleSession(worktree, AgentRole.IMPLEMENTER).session_uuid()
    assert str(uuid.UUID(result)) == result


# ── RoleSession lifecycle ─────────────────────────────────────────────────────


def test_fresh_worktree_reports_fresh(rs):
    assert rs.run_kind() == RunKind.FRESH
    assert rs.is_resumable() is False
    assert rs.is_done() is False


def test_populated_dir_is_resumable(rs):
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n")

    assert rs.run_kind() == RunKind.RESUME
    assert rs.is_resumable() is True
    assert rs.is_done() is False


def test_mark_done_signals_done_dir_survives_next_session_is_fresh(rs, worktree):
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n")
    rs.mark_done()

    assert rs.is_done() is True
    assert rs.is_resumable() is False
    assert rs.path.is_dir()
    assert RoleSession(worktree, AgentRole.IMPLEMENTER).run_kind() == RunKind.FRESH


def test_mark_done_removes_readonly_files(rs):
    rs.start_fresh()
    pack_dir = rs.path / "codex" / ".tmp" / "plugins" / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    pack_file = pack_dir / "pack-abc123.pack"
    pack_file.write_bytes(b"data")
    os.chmod(pack_file, stat.S_IREAD)

    rs.mark_done()

    assert rs.is_done() is True
    assert rs.is_resumable() is False


def test_start_fresh_on_populated_dir_makes_not_resumable(rs):
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n")
    rs.start_fresh()

    assert rs.is_resumable() is False


def test_service_session_ids_are_isolated_by_role_and_worktree(tmp_path):
    planner_a = RoleSession(tmp_path / "worktree-a", AgentRole.PLANNER)
    planner_b = RoleSession(tmp_path / "worktree-b", AgentRole.PLANNER)
    reviewer_a = RoleSession(tmp_path / "worktree-a", AgentRole.REVIEWER)

    planner_a.save_service_session_id("opencode", "sess-a")
    planner_b.save_service_session_id("opencode", "sess-b")
    reviewer_a.save_service_session_id("opencode", "sess-review")

    assert planner_a.service_session_id("opencode") == "sess-a"
    assert planner_b.service_session_id("opencode") == "sess-b"
    assert reviewer_a.service_session_id("opencode") == "sess-review"


def test_service_session_ids_use_service_specific_sidecars(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)

    rs.save_service_session_id("codex", "thread-123")
    rs.save_service_session_id("opencode", "sess-123")
    rs.save_service_session_id("unknown-service", "default-123")

    assert rs.service_session_id("codex") == "thread-123"
    assert rs.service_session_id("opencode") == "sess-123"
    assert rs.service_session_id("unknown-service") == "default-123"


def test_provider_identity_recovers_single_nested_codex_rollout_thread_id_and_persists_it(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    state_dir = rs.path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30" / "nested"
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

    identity = rs.provider_identity(
        "codex",
        has_resumable_provider_state=True,
        provider_state_dir=state_dir,
    )

    assert identity.kind is ProviderIdentityKind.RESUME
    assert identity.run_kind is RunKind.RESUME
    assert identity.provider_session_id == "thread-from-rollout"
    assert identity.persist_provider_session_id is True
    assert rs.service_session_id("codex") == "thread-from-rollout"


def test_provider_identity_provider_run_state_preserves_provider_state_dir_and_session_id(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    provider_state_dir = rs.path / "codex"
    provider_state_dir.mkdir(parents=True)
    provider_state_dir.joinpath("thread_id").write_text(
        "thread-from-sidecar\n",
        encoding="utf-8",
    )

    provider_run_state = rs.provider_identity(
        "codex",
        has_resumable_provider_state=True,
        provider_state_dir=provider_state_dir,
    ).provider_run_state(provider_state_dir=provider_state_dir)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.RESUME,
        provider_session_id="thread-from-sidecar",
        provider_state_dir=provider_state_dir,
    )


def test_provider_identity_provider_run_state_reports_unrecoverable_fallback_reason(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    provider_state_dir = rs.path / "codex"
    dir_a = provider_state_dir / "sessions" / "2026" / "05" / "30"
    dir_b = provider_state_dir / "sessions" / "2026" / "05" / "31"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    dir_a.joinpath("rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-old"}\n',
        encoding="utf-8",
    )
    dir_b.joinpath("rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-new"}\n',
        encoding="utf-8",
    )

    provider_run_state = rs.provider_identity(
        "codex",
        has_resumable_provider_state=True,
        provider_state_dir=provider_state_dir,
    ).provider_run_state(provider_state_dir=provider_state_dir)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.FRESH,
        provider_session_id=None,
        provider_state_dir=provider_state_dir,
        fresh_fallback_reason=(ProviderFreshFallbackReason.UNRECOVERABLE_IDENTITY),
    )


def test_provider_run_state_for_non_codex_service_is_fresh_without_provider_session_id_when_state_dir_is_not_resumable(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    service = _FakeService(
        name="opencode",
        relpath="custom/opencode-state/",
        resumable=False,
    )

    provider_run_state = rs.provider_run_state_for_service(service)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.FRESH,
        provider_session_id=None,
        provider_state_dir=worktree / "custom" / "opencode-state",
    )


def test_provider_run_state_for_claude_service_resumes_with_role_session_uuid_without_sidecar(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    service = _FakeService(
        name="claude",
        relpath="custom/claude-state/",
        resumable=True,
    )
    provider_state_dir = worktree / "custom" / "claude-state"
    provider_state_dir.mkdir(parents=True)
    provider_state_dir.joinpath("session.jsonl").write_text("{}\n", encoding="utf-8")

    provider_run_state = rs.provider_run_state_for_service(service)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.RESUME,
        provider_session_id=rs.session_uuid(),
        provider_state_dir=provider_state_dir,
    )


def test_provider_run_state_for_codex_service_prefers_saved_thread_id_without_sessions_dir(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    service = CodexService()
    provider_state_dir = worktree / ".pycastle-session" / "implementer" / "codex"
    provider_state_dir.mkdir(parents=True)
    rs.save_service_session_id("codex", "thread-from-sidecar")

    provider_run_state = rs.provider_run_state_for_service(service)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.RESUME,
        provider_session_id="thread-from-sidecar",
        provider_state_dir=provider_state_dir,
    )


def test_provider_run_state_for_codex_service_is_fresh_when_rollouts_are_unreadable(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    service = CodexService()
    provider_state_dir = worktree / ".pycastle-session" / "implementer" / "codex"
    rollout_path = (
        provider_state_dir / "sessions" / "2026" / "05" / "31" / "rollout-001.jsonl"
    )
    rollout_path.parent.mkdir(parents=True)
    rollout_path.write_bytes(b"\xff\xfe\x00")

    provider_run_state = rs.provider_run_state_for_service(service)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.FRESH,
        provider_session_id=None,
        provider_state_dir=provider_state_dir,
        fresh_fallback_reason=ProviderFreshFallbackReason.UNRECOVERABLE_IDENTITY,
    )


def test_provider_run_state_for_sidecar_backed_service_resumes_with_saved_service_session_id(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    service = _FakeService(
        name="opencode",
        relpath="custom/opencode-state/",
        resumable=True,
    )
    provider_state_dir = worktree / "custom" / "opencode-state"
    provider_state_dir.mkdir(parents=True)
    provider_state_dir.joinpath("session_id").write_text(
        "sess-opencode-123\n",
        encoding="utf-8",
    )
    rs.save_service_session_id("opencode", "sess-opencode-123")

    provider_run_state = rs.provider_run_state_for_service(service)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.RESUME,
        provider_session_id="sess-opencode-123",
        provider_state_dir=provider_state_dir,
    )


def test_provider_run_state_for_sidecar_backed_service_falls_back_to_fresh_without_inventing_session_id(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    service = _FakeService(
        name="opencode",
        relpath="custom/opencode-state/",
        resumable=True,
    )
    provider_state_dir = worktree / "custom" / "opencode-state"
    provider_state_dir.mkdir(parents=True)

    provider_run_state = rs.provider_run_state_for_service(service)

    assert provider_run_state == ProviderRunState(
        run_kind=RunKind.FRESH,
        provider_session_id=None,
        provider_state_dir=provider_state_dir,
        fresh_fallback_reason=ProviderFreshFallbackReason.UNRECOVERABLE_IDENTITY,
    )


def test_mark_done_preserves_service_session_metadata_without_counting_as_resumable(rs):
    rs.start_fresh()
    rs.save_service_session_metadata("codex", "thread-from-run")
    rs.save_service_session_id("codex", "thread-from-run")

    rs.mark_done()

    assert rs.service_session_metadata("codex") == {
        "service": "codex",
        "provider_session_id": "thread-from-run",
    }
    assert rs.is_done() is True
    assert rs.is_resumable() is False
    assert rs.run_kind() == RunKind.FRESH


def test_malformed_service_session_metadata_is_ignored(rs):
    rs.start_fresh()
    service_session_metadata_path(rs.path).write_text("{not-json", encoding="utf-8")

    assert rs.service_session_metadata("claude") is None
    assert rs.exact_transcript_service_name() is None
    assert rs.is_resumable() is False
    assert rs.run_kind() == RunKind.FRESH


def test_exact_transcript_service_name_is_ambiguous_with_multiple_services(rs):
    rs.start_fresh()
    rs.save_service_session_metadata("claude", "thread-claude")
    rs.save_service_session_metadata("opencode", "sess-opencode")

    assert rs.exact_transcript_service_name() is None


def test_role_session_reports_exact_provider_transcript_available_for_selected_opencode_service(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.REVIEWER, "main")
    service = _FakeService(
        name="opencode",
        relpath="custom/opencode-state/",
        resumable=True,
    )
    state_dir = worktree / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    state_dir.joinpath("session_id").write_text(
        "sess-opencode-123\n",
        encoding="utf-8",
    )
    rs.save_service_session_id("opencode", "sess-opencode-123")
    rs.save_service_session_metadata("opencode", "sess-opencode-123")
    registry = ServiceRegistry({"opencode": cast(AgentService, service)})

    assert rs.has_exact_provider_transcript_for_service(service) is True
    assert (
        rs.has_exact_transcript_handoff_for_selected_service(registry, "opencode")
        is True
    )


@pytest.mark.parametrize(
    ("registry_services", "selected_service_name"),
    [
        ({}, "codex"),
        (
            {
                "claude": _FakeService(
                    name="claude",
                    relpath="custom/claude-state/",
                    resumable=True,
                )
            },
            "claude",
        ),
    ],
)
def test_role_session_reports_exact_provider_transcript_unavailable_for_missing_or_different_selected_service(
    worktree,
    registry_services: dict[str, _FakeService],
    selected_service_name: str,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    state_dir = rs.path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    rollout_dir.joinpath("rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-exact"}\n',
        encoding="utf-8",
    )
    rs.save_service_session_id("codex", "thread-exact")
    rs.save_service_session_metadata("codex", "thread-exact")
    registry = ServiceRegistry(cast(dict[str, AgentService], registry_services))

    assert (
        rs.has_exact_provider_transcript_for_selected_service(
            registry,
            selected_service_name,
        )
        is False
    )
    assert (
        rs.has_exact_transcript_handoff_for_selected_service(
            registry,
            selected_service_name,
        )
        is False
    )


def test_role_session_reports_exact_transcript_handoff_unavailable_for_ambiguous_codex_identity(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    state_dir = rs.path / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "30"
    dir_b = state_dir / "sessions" / "2026" / "05" / "31"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    dir_a.joinpath("rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-old"}\n',
        encoding="utf-8",
    )
    dir_b.joinpath("rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-new"}\n',
        encoding="utf-8",
    )
    rs.save_service_session_id("codex", "thread-old")
    rs.save_service_session_metadata("codex", "thread-old")
    registry = ServiceRegistry({"codex": CodexService()})

    assert (
        rs.has_exact_provider_transcript_for_selected_service(registry, "codex")
        is False
    )
    assert (
        rs.has_exact_transcript_handoff_for_selected_service(registry, "codex") is False
    )


@pytest.mark.parametrize(
    ("metadata_value", "sidecar_value", "resumable"),
    [
        (None, "sess-opencode-123", True),
        ("sess-opencode-123", None, True),
        ("sess-opencode-metadata", "sess-opencode-sidecar", True),
        ("sess-opencode-123", "sess-opencode-123", False),
    ],
)
def test_role_session_reports_exact_provider_transcript_unavailable_without_exact_identity_evidence(
    worktree,
    metadata_value: str | None,
    sidecar_value: str | None,
    resumable: bool,
):
    rs = RoleSession(worktree, AgentRole.REVIEWER, "main")
    service = _FakeService(
        name="opencode",
        relpath="custom/opencode-state/",
        resumable=resumable,
    )
    state_dir = worktree / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    state_dir.joinpath("session_id").write_text(
        "sess-opencode-123\n",
        encoding="utf-8",
    )
    if sidecar_value is not None:
        rs.save_service_session_id("opencode", sidecar_value)
    if metadata_value is not None:
        rs.save_service_session_metadata("opencode", metadata_value)

    assert (
        rs.has_exact_provider_transcript_for_service(cast(AgentService, service))
        is False
    )


def test_role_session_reports_exact_provider_transcript_codex_availability_for_duplicate_and_ambiguous_rollouts(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    service = CodexService()
    state_dir = rs.path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    rollout_path = rollout_dir / "rollout-001.jsonl"

    rollout_path.write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-exact"}',
                '{"type":"thread.started","thread_id":"thread-exact"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rs.save_service_session_id("codex", "thread-exact")
    rs.save_service_session_metadata("codex", "thread-exact")

    assert rs.has_exact_provider_transcript_for_service(service) is True

    rollout_path.write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-exact"}',
                '{"type":"thread.started","thread_id":"thread-other"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert rs.has_exact_provider_transcript_for_service(service) is False


# ── any_role_dir_present ──────────────────────────────────────────────────────


def test_any_role_dir_present_false_when_no_session_base(worktree):
    assert any_role_dir_present(worktree) is False


def test_any_role_dir_present_true_once_a_role_dir_exists(worktree):
    RoleSession(worktree, AgentRole.IMPLEMENTER).start_fresh()
    assert any_role_dir_present(worktree) is True


def test_any_role_dir_present_true_regardless_of_done_state(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    rs.start_fresh()
    rs.mark_done()
    assert any_role_dir_present(worktree) is True


# ── is_stage_done_for ─────────────────────────────────────────────────────────


def test_is_stage_done_for_false_when_absent(worktree):
    assert is_stage_done_for(worktree, AgentRole.IMPLEMENTER) is False


def test_is_stage_done_for_true_after_mark_done(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n")
    rs.mark_done()
    assert is_stage_done_for(worktree, AgentRole.IMPLEMENTER) is True


# ── RoleSession.discard() ─────────────────────────────────────────────────────


def test_discard_after_start_fresh_removes_role_dir(rs, worktree):
    rs.start_fresh()
    rs.discard()

    assert rs.is_resumable() is False
    assert rs.is_done() is False
    assert any_role_dir_present(worktree) is False


def test_discard_removes_nested_contents(rs, worktree):
    rs.start_fresh()
    nested = rs.path / "subdir"
    nested.mkdir()
    (nested / "file.txt").write_text("data")
    rs.discard()

    assert rs.is_resumable() is False
    assert rs.is_done() is False


def test_discard_on_nonexistent_dir_is_noop(rs):
    rs.discard()  # no start_fresh — dir never created


def test_discard_is_idempotent(rs, worktree):
    rs.start_fresh()
    rs.discard()
    rs.discard()

    assert rs.is_resumable() is False
    assert rs.is_done() is False


def test_discard_sibling_safe(worktree):
    rs_impl = RoleSession(worktree, AgentRole.IMPLEMENTER)
    rs_review = RoleSession(worktree, AgentRole.REVIEWER)
    rs_impl.start_fresh()
    rs_review.start_fresh()

    rs_impl.discard()

    assert any_role_dir_present(worktree) is True
    assert rs_review.is_resumable() is False
    assert rs_review.is_done() is True
