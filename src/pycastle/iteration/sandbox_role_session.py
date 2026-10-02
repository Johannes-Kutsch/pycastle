from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pycastle.agents.output_protocol import AgentRole
from pycastle.infrastructure.worktree import (
    SandboxWorktreeIntent,
    replaceable_merge_sandbox_worktree,
    reusable_sandbox_worktree,
    reusable_sandbox_worktree_identity,
    worktree_identity,
)
from pycastle.iteration._fingerprint import prepare_fingerprint_gate
from pycastle.managed_worktree_mount_policy import guard_managed_worktree_mount
from pycastle.session import RoleSession

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from pycastle.services import GitService


class _SandboxEntryDeps(Protocol):
    repo_root: Path
    git_svc: GitService


@dataclass(frozen=True)
class ReusableSandboxKind:
    intent: SandboxWorktreeIntent
    role: AgentRole


@dataclass(frozen=True)
class MergerSandboxKind:
    issue_number: int


@asynccontextmanager
async def reusable_sandbox_entry(
    intent: SandboxWorktreeIntent,
    *,
    fingerprint: str,
    role: AgentRole,
    deps: _SandboxEntryDeps,
    sha: str | None,
    operating_branch: str,
) -> AsyncIterator[tuple[Path, RoleSession]]:
    identity = reusable_sandbox_worktree_identity(intent, deps.repo_root)
    role_session = RoleSession(identity.path, role)
    prepare_fingerprint_gate(role_session, fingerprint)
    async with reusable_sandbox_worktree(
        intent,
        sha=sha,
        deps=deps,
        operating_branch=operating_branch,
    ) as sandbox_path:
        role_session = RoleSession(sandbox_path, role)
        role_session.write_fingerprint(fingerprint)
        guard_managed_worktree_mount(
            repo_root=deps.repo_root,
            mount_path=sandbox_path,
            caller=role.value,
            role=role.value,
        )
        yield sandbox_path, role_session


@asynccontextmanager
async def merger_sandbox_entry(
    issue_number: int,
    *,
    fingerprint: str,
    deps: _SandboxEntryDeps,
    sha: str | None,
    operating_branch: str,
) -> AsyncIterator[tuple[Path, RoleSession]]:
    role = AgentRole.MERGER
    identity = worktree_identity(
        f"pycastle/merge-sandbox-issue-{issue_number}",
        deps.repo_root,
    )
    role_session = RoleSession(identity.path, role)
    prepare_fingerprint_gate(role_session, fingerprint)
    if role_session.is_resumable():
        worktree_cm = reusable_sandbox_worktree(
            f"merge-sandbox-issue-{issue_number}",
            sha=sha,
            deps=deps,
            operating_branch=operating_branch,
        )
    else:
        worktree_cm = replaceable_merge_sandbox_worktree(
            issue_number=issue_number,
            sha=sha,
            deps=deps,
            operating_branch=operating_branch,
        )
    async with worktree_cm as sandbox_path:
        role_session = RoleSession(sandbox_path, role)
        role_session.write_fingerprint(fingerprint)
        guard_managed_worktree_mount(
            repo_root=deps.repo_root,
            mount_path=sandbox_path,
            caller=role.value,
            role=role.value,
        )
        yield sandbox_path, role_session


@asynccontextmanager
async def sandbox_entry(
    kind: ReusableSandboxKind | MergerSandboxKind,
    *,
    fingerprint: str,
    deps: _SandboxEntryDeps,
    sha: str | None,
    operating_branch: str,
) -> AsyncIterator[tuple[Path, RoleSession]]:
    if isinstance(kind, ReusableSandboxKind):
        role = kind.role
        identity = reusable_sandbox_worktree_identity(kind.intent, deps.repo_root)
    else:
        role = AgentRole.MERGER
        identity = worktree_identity(
            f"pycastle/merge-sandbox-issue-{kind.issue_number}",
            deps.repo_root,
        )

    pre_role_session = RoleSession(identity.path, role)
    prepare_fingerprint_gate(pre_role_session, fingerprint)

    if isinstance(kind, ReusableSandboxKind):
        worktree_cm = reusable_sandbox_worktree(
            kind.intent,
            sha=sha,
            deps=deps,
            operating_branch=operating_branch,
        )
    elif pre_role_session.is_resumable():
        worktree_cm = reusable_sandbox_worktree(
            f"merge-sandbox-issue-{kind.issue_number}",
            sha=sha,
            deps=deps,
            operating_branch=operating_branch,
        )
    else:
        worktree_cm = replaceable_merge_sandbox_worktree(
            issue_number=kind.issue_number,
            sha=sha,
            deps=deps,
            operating_branch=operating_branch,
        )

    async with worktree_cm as sandbox_path:
        role_session = RoleSession(sandbox_path, role)
        role_session.write_fingerprint(fingerprint)
        guard_managed_worktree_mount(
            repo_root=deps.repo_root,
            mount_path=sandbox_path,
            caller=role.value,
            role=role.value,
        )
        yield sandbox_path, role_session
