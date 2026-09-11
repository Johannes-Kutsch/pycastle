from __future__ import annotations

import pytest

from pycastle.errors import SetupPhaseError
from pycastle.managed_worktree_mount_policy import (
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    guard_managed_worktree_mount,
)


def _make_worktrees_dir(tmp_path):
    worktrees_dir = tmp_path / "pycastle" / ".worktrees"
    worktrees_dir.mkdir(parents=True)
    return worktrees_dir


def test_guard_hard_reject_raises_setup_phase_error(tmp_path):
    worktrees_dir = _make_worktrees_dir(tmp_path)
    invalid_mount = tmp_path / "outside-worktrees" / "sandbox"
    invalid_mount.mkdir(parents=True)

    with pytest.raises(SetupPhaseError):
        guard_managed_worktree_mount(
            repo_root=tmp_path,
            mount_path=invalid_mount,
            caller="Test Caller",
            role="test_role",
        )

    _ = worktrees_dir  # created so should_reject returns True


def test_guard_hard_reject_message_equals_describe_rejection(tmp_path):
    _make_worktrees_dir(tmp_path)
    invalid_mount = tmp_path / "outside-worktrees" / "sandbox"
    invalid_mount.mkdir(parents=True)

    rejection = decide_managed_worktree_mount(
        repo_root=tmp_path,
        mount_path=invalid_mount,
        caller="Test Caller",
        role="test_role",
    )
    expected_message = describe_managed_worktree_mount_rejection(rejection)

    with pytest.raises(SetupPhaseError) as exc_info:
        guard_managed_worktree_mount(
            repo_root=tmp_path,
            mount_path=invalid_mount,
            caller="Test Caller",
            role="test_role",
        )

    assert str(exc_info.value) == expected_message


def test_guard_hard_reject_phase_equals_rejection_role(tmp_path):
    _make_worktrees_dir(tmp_path)
    invalid_mount = tmp_path / "outside-worktrees" / "sandbox"
    invalid_mount.mkdir(parents=True)

    with pytest.raises(SetupPhaseError) as exc_info:
        guard_managed_worktree_mount(
            repo_root=tmp_path,
            mount_path=invalid_mount,
            caller="Test Caller",
            role="my_role",
        )

    assert exc_info.value.phase == "my_role"


def test_guard_hard_reject_phase_falls_back_to_caller_role_when_rejection_role_is_none(
    tmp_path,
):
    _make_worktrees_dir(tmp_path)
    invalid_mount = tmp_path / "outside-worktrees" / "sandbox"
    invalid_mount.mkdir(parents=True)

    with pytest.raises(SetupPhaseError) as exc_info:
        guard_managed_worktree_mount(
            repo_root=tmp_path,
            mount_path=invalid_mount,
            caller="Test Caller",
            role="fallback_role",
        )

    assert exc_info.value.phase == "fallback_role"


def test_guard_returns_without_raising_on_accept(tmp_path):
    worktrees_dir = _make_worktrees_dir(tmp_path)
    valid_mount = worktrees_dir / "my-sandbox"
    valid_mount.mkdir()

    guard_managed_worktree_mount(
        repo_root=tmp_path,
        mount_path=valid_mount,
        caller="Test Caller",
        role="my_role",
    )


def test_guard_returns_without_raising_on_soft_reject(tmp_path):
    # worktrees dir is absent — should_reject_managed_worktree_mount returns False
    invalid_mount = tmp_path / "outside-worktrees" / "sandbox"
    invalid_mount.mkdir(parents=True)

    guard_managed_worktree_mount(
        repo_root=tmp_path,
        mount_path=invalid_mount,
        caller="Test Caller",
        role="my_role",
    )
