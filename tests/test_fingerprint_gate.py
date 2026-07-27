"""Tests for prepare_fingerprint_gate helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.session import RoleSession


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def rs(worktree: Path) -> RoleSession:
    return RoleSession(worktree, AgentRole.IMPLEMENTER)


# ── Behavior 1: no fingerprint stored → discard() is called ──────────────────


def test_prepare_fingerprint_gate_discards_when_no_fingerprint_stored(
    rs: RoleSession,
) -> None:
    from pycastle.iteration._fingerprint import prepare_fingerprint_gate

    rs.start_fresh()
    (rs.path / "_continuation").write_text("opaque-token", encoding="utf-8")
    assert rs.path.is_dir()

    prepare_fingerprint_gate(rs, "sha-abc123")

    assert not rs.path.is_dir()


# ── Behavior 2: fingerprint matches → discard() is NOT called ─────────────────


def test_prepare_fingerprint_gate_leaves_session_intact_when_fingerprint_matches(
    rs: RoleSession,
) -> None:
    from pycastle.iteration._fingerprint import prepare_fingerprint_gate

    rs.write_fingerprint("sha-abc123")
    (rs.path / "_continuation").write_text("opaque-token", encoding="utf-8")

    prepare_fingerprint_gate(rs, "sha-abc123")

    assert rs.path.is_dir()
    assert rs.is_resumable()


# ── Behavior 3: fingerprint differs → discard() is called ────────────────────


def test_prepare_fingerprint_gate_discards_when_fingerprint_differs(
    rs: RoleSession,
) -> None:
    from pycastle.iteration._fingerprint import prepare_fingerprint_gate

    rs.write_fingerprint("sha-old")
    (rs.path / "_continuation").write_text("opaque-token", encoding="utf-8")

    prepare_fingerprint_gate(rs, "sha-new")

    assert not rs.path.is_dir()
