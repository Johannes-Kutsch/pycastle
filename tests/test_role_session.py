from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.session.role import RoleSession

if TYPE_CHECKING:
    from pathlib import Path


def _make_resumable(session: RoleSession) -> None:
    session.path.mkdir(parents=True, exist_ok=True)
    session.write_continuation("continuation-data")


def test_fork_namespace_copies_session_tree_including_continuation(
    tmp_path: Path,
) -> None:
    source = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    _make_resumable(source)
    extra = source.path / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    fork = source.fork_namespace("fork-a")

    assert fork.path.is_dir()
    assert (fork.path / "_continuation").read_text(
        encoding="utf-8"
    ) == "continuation-data"
    assert (fork.path / "extra.txt").read_text(encoding="utf-8") == "extra"


def test_fork_namespace_produces_resumable_session(tmp_path: Path) -> None:
    source = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    _make_resumable(source)

    fork = source.fork_namespace("fork-b")

    assert fork.is_resumable()


def test_fork_namespace_discarding_fork_leaves_source_intact(tmp_path: Path) -> None:
    source = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    _make_resumable(source)
    fork = source.fork_namespace("fork-c")

    fork.discard()

    assert source.path.is_dir()
    assert source.is_resumable()


def test_fork_namespace_discarding_source_leaves_fork_intact(tmp_path: Path) -> None:
    source = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    _make_resumable(source)
    fork = source.fork_namespace("fork-d")

    source.discard()

    assert fork.path.is_dir()
    assert fork.is_resumable()


def test_fork_namespace_raises_when_target_already_exists(tmp_path: Path) -> None:
    source = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    _make_resumable(source)
    existing = RoleSession(tmp_path, AgentRole.IMPROVE, "already")
    existing.path.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="already exists"):
        source.fork_namespace("already")


def test_fork_namespace_raises_when_source_does_not_exist(tmp_path: Path) -> None:
    source = RoleSession(tmp_path, AgentRole.IMPROVE, "nonexistent")

    with pytest.raises(ValueError, match="does not exist"):
        source.fork_namespace("fork-e")
