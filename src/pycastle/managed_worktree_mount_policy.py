from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from .errors import ManagedWorktreeMountPreconditionError
from .infrastructure.worktree import PROJECT_LOCAL_PYCASTLE_DIR


@dataclass(frozen=True)
class ManagedWorktreeMountAccepted:
    caller: str
    role: str | None
    repo_root: Path
    mount_path: Path
    expected_worktrees_dir: Path


@dataclass(frozen=True)
class ManagedWorktreeMountRejected:
    caller: str
    role: str | None
    repo_root: Path
    mount_path: Path
    expected_worktrees_dir: Path
    expected_mount_path: Path
    rejection_code: str
    invariant: str
    detail: str
    actual_parent: Path


ManagedWorktreeMountDecision: TypeAlias = (
    ManagedWorktreeMountAccepted | ManagedWorktreeMountRejected
)

_MANAGED_WORKTREE_INVARIANT = (
    "mount path must be an existing managed worktree directory directly under "
    "<repo>/pycastle/.worktrees/"
)


def decide_managed_worktree_mount(
    *,
    repo_root: Path,
    mount_path: Path,
    caller: str,
    role: str | None = None,
) -> ManagedWorktreeMountDecision:
    expected_worktrees_dir = repo_root / PROJECT_LOCAL_PYCASTLE_DIR / ".worktrees"
    expected_mount_path = expected_worktrees_dir / mount_path.name
    actual_parent = mount_path.parent
    if actual_parent != expected_worktrees_dir:
        return ManagedWorktreeMountRejected(
            caller=caller,
            role=role,
            repo_root=repo_root,
            mount_path=mount_path,
            expected_worktrees_dir=expected_worktrees_dir,
            expected_mount_path=expected_mount_path,
            rejection_code="invalid_mount_path",
            invariant=_MANAGED_WORKTREE_INVARIANT,
            detail=f"Expected parent {expected_worktrees_dir}, got {actual_parent}.",
            actual_parent=actual_parent,
        )
    if not mount_path.exists():
        return ManagedWorktreeMountRejected(
            caller=caller,
            role=role,
            repo_root=repo_root,
            mount_path=mount_path,
            expected_worktrees_dir=expected_worktrees_dir,
            expected_mount_path=mount_path,
            rejection_code="missing_mount_path",
            invariant=_MANAGED_WORKTREE_INVARIANT,
            detail="Expected managed worktree path does not exist.",
            actual_parent=actual_parent,
        )
    return ManagedWorktreeMountAccepted(
        caller=caller,
        role=role,
        repo_root=repo_root,
        mount_path=mount_path,
        expected_worktrees_dir=expected_worktrees_dir,
    )


def describe_managed_worktree_mount_rejection(
    rejection: ManagedWorktreeMountRejected,
) -> str:
    role_text = f" role {rejection.role!r}" if rejection.role else ""
    return (
        f"{rejection.caller}{role_text} requires a managed worktree mount. "
        f"Invariant: {rejection.invariant}. "
        f"Expected worktrees dir: {rejection.expected_worktrees_dir}. "
        f"Expected mount path: {rejection.expected_mount_path}. "
        f"Got mount path: {rejection.mount_path}. "
        f"Reason: {rejection.rejection_code}. "
        f"{rejection.detail}"
    )


def should_reject_managed_worktree_mount(
    decision: ManagedWorktreeMountDecision,
) -> bool:
    if not isinstance(decision, ManagedWorktreeMountRejected):
        return False
    return decision.expected_worktrees_dir.exists() and decision.mount_path.exists()


def infer_repo_root_for_mount_path(mount_path: Path) -> Path:
    resolved = mount_path.resolve(strict=False)
    if (
        resolved.parent.name == ".worktrees"
        and resolved.parent.parent.name == PROJECT_LOCAL_PYCASTLE_DIR
    ):
        return resolved.parent.parent.parent
    for candidate in (resolved, *resolved.parents):
        if (candidate / PROJECT_LOCAL_PYCASTLE_DIR / ".worktrees").exists():
            return candidate
    return resolved if resolved.is_dir() else resolved.parent


def enforce_managed_worktree_mount(
    *,
    mount_path: Path,
    caller: str,
    role: str | None = None,
) -> ManagedWorktreeMountAccepted:
    decision = decide_managed_worktree_mount(
        repo_root=infer_repo_root_for_mount_path(mount_path),
        mount_path=mount_path,
        caller=caller,
        role=role,
    )
    if isinstance(decision, ManagedWorktreeMountRejected):
        raise ManagedWorktreeMountPreconditionError(
            describe_managed_worktree_mount_rejection(decision),
            rejection_code=decision.rejection_code,
        )
    return decision
