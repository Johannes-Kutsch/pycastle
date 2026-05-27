"""Tests for session_resume: RoleSession lifecycle and module-level helpers."""

import os
import stat
import uuid

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.session import (
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
