"""Tests for session_resume: RoleSession lifecycle and module-level helpers."""

import os
import stat
import uuid

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.session import (
    ProviderIdentity,
    ProviderIdentityKind,
    RoleSession,
    RunKind,
    any_role_dir_present,
    is_stage_done_for,
)


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


def test_service_session_id_filenames_remain_byte_compatible(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)

    assert rs.service_session_id_path("codex").name == "thread_id"
    assert rs.service_session_id_path("opencode").name == "session_id"
    assert rs.service_session_id_path("unknown-service").name == "thread_id"


def test_provider_identity_resumes_from_saved_codex_thread_id(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    rs.save_service_session_id("codex", "thread-from-sidecar")

    assert rs.provider_identity("codex", has_resumable_provider_state=True) == (
        ProviderIdentity(
            kind=ProviderIdentityKind.RESUME,
            run_kind=RunKind.RESUME,
            provider_session_id="thread-from-sidecar",
        )
    )


def test_provider_identity_is_unrecoverable_when_opencode_sidecar_session_id_is_missing(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")

    assert rs.provider_identity("opencode", has_resumable_provider_state=True) == (
        ProviderIdentity(
            kind=ProviderIdentityKind.UNRECOVERABLE,
            run_kind=RunKind.FRESH,
            provider_session_id=None,
        )
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
    rs.service_session_metadata_path.write_text("{not-json", encoding="utf-8")

    assert rs.service_session_metadata("claude") is None
    assert rs.is_resumable() is False
    assert rs.run_kind() == RunKind.FRESH


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
