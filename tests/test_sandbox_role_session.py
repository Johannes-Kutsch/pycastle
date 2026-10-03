"""Tests for sandbox_role_session entry context managers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.errors import SetupPhaseError
from pycastle.infrastructure.worktree import (
    SandboxWorktreeIntent,
    reusable_sandbox_worktree_identity,
    worktree_identity,
)
from pycastle.iteration.sandbox_role_session import (
    MergerSandboxKind,
    ReusableSandboxKind,
    sandbox_entry,
)
from pycastle.session import RoleSession
from tests.support import FakeAgentRunner, _make_deps, functional_git_svc

if TYPE_CHECKING:
    from pathlib import Path

FINGERPRINT = "abc123fingerprint"
OLD_FINGERPRINT = "oldfingerprint"
ISSUE_NUMBER = 42
OPERATING_BRANCH = "main"
PLAN_BRANCH = "pycastle/plan-sandbox"
MERGER_BRANCH = f"pycastle/merge-sandbox-issue-{ISSUE_NUMBER}"


@pytest.fixture
def git_svc():
    svc = functional_git_svc()
    svc.is_working_tree_clean.return_value = True
    return svc


@pytest.fixture
def deps(tmp_path, git_svc):
    return _make_deps(tmp_path, FakeAgentRunner([]), git_svc=git_svc)


def _plan_sandbox_path(deps) -> Path:
    return reusable_sandbox_worktree_identity(
        SandboxWorktreeIntent.PLAN, deps.repo_root
    ).path


def _merger_sandbox_path(deps) -> Path:
    return worktree_identity(MERGER_BRANCH, deps.repo_root).path


def _seed_worktree(git_svc, repo_root, path, branch):
    """Create a managed worktree using the fake git_svc (creates dir + registers it)."""
    git_svc.create_worktree(repo_root, path, branch)


def _write_session(path, role, *, fingerprint=None, with_continuation=False):
    rs = RoleSession(path, role)
    if fingerprint is not None:
        rs.write_fingerprint(fingerprint)
    if with_continuation:
        rs.write_continuation("opaque-continuation")
    return rs


# ── sandbox_entry (unified entry) ────────────────────────────────────────────


# AC 2: reusable discriminator + fingerprint match + continuation → resumable


def test_sandbox_entry_reusable_kind_fingerprint_match_yields_resumable_session(
    deps, git_svc
):
    path = _plan_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, PLAN_BRANCH)
    _write_session(
        path, AgentRole.PLANNER, fingerprint=FINGERPRINT, with_continuation=True
    )
    git_svc.get_current_branch.return_value = PLAN_BRANCH

    async def run():
        async with sandbox_entry(
            ReusableSandboxKind(SandboxWorktreeIntent.PLAN, AgentRole.PLANNER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (_sandbox_path, role_session):
            assert role_session.is_resumable()

    asyncio.run(run())


# AC 2: reusable discriminator + fingerprint mismatch → non-resumable


def test_sandbox_entry_reusable_kind_fingerprint_mismatch_yields_non_resumable_session(
    deps, git_svc
):
    path = _plan_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, PLAN_BRANCH)
    _write_session(
        path, AgentRole.PLANNER, fingerprint=OLD_FINGERPRINT, with_continuation=True
    )

    async def run():
        async with sandbox_entry(
            ReusableSandboxKind(SandboxWorktreeIntent.PLAN, AgentRole.PLANNER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (_sandbox_path, role_session):
            assert not role_session.is_resumable()

    asyncio.run(run())


# AC 5: reusable discriminator writes fingerprint to live RoleSession


def test_sandbox_entry_reusable_kind_writes_fingerprint(deps):
    async def run():
        async with sandbox_entry(
            ReusableSandboxKind(SandboxWorktreeIntent.PLAN, AgentRole.PLANNER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (_sandbox_path, role_session):
            assert role_session.read_fingerprint() == FINGERPRINT

    asyncio.run(run())


# AC 3: unified entry yields RoleSession at sandbox_path for given role


def test_sandbox_entry_yields_role_session_at_sandbox_path(deps):
    async def run():
        async with sandbox_entry(
            ReusableSandboxKind(SandboxWorktreeIntent.PLAN, AgentRole.PLANNER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, role_session):
            assert isinstance(role_session, RoleSession)
            assert role_session.path == sandbox_path / ".pycastle-session" / "planner"

    asyncio.run(run())


# AC 3: merger discriminator uses pycastle/merge-sandbox-issue-N naming


def test_sandbox_entry_merger_kind_sandbox_path_uses_issue_branch_naming(deps):
    async def run():
        async with sandbox_entry(
            MergerSandboxKind(ISSUE_NUMBER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, _role_session):
            assert sandbox_path == _merger_sandbox_path(deps)

    asyncio.run(run())


# AC 4: merger discriminator + resumable pre-session → reusable opener (preserves content)


def test_sandbox_entry_merger_kind_resumable_pre_session_preserves_worktree(
    deps, git_svc
):
    path = _merger_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, MERGER_BRANCH)
    _write_session(
        path, AgentRole.MERGER, fingerprint=FINGERPRINT, with_continuation=True
    )
    git_svc.get_current_branch.return_value = MERGER_BRANCH
    (path / "prior_work.txt").write_text("preserved content")

    async def run():
        async with sandbox_entry(
            MergerSandboxKind(ISSUE_NUMBER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, _role_session):
            assert (sandbox_path / "prior_work.txt").exists()

    asyncio.run(run())


# AC 4: merger discriminator + fingerprint mismatch → replaceable opener (discards content)


def test_sandbox_entry_merger_kind_fingerprint_mismatch_rebuilds_worktree(
    deps, git_svc
):
    path = _merger_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, MERGER_BRANCH)
    _write_session(
        path, AgentRole.MERGER, fingerprint=OLD_FINGERPRINT, with_continuation=True
    )
    (path / "prior_work.txt").write_text("stale content")

    async def run():
        async with sandbox_entry(
            MergerSandboxKind(ISSUE_NUMBER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, _role_session):
            assert not (sandbox_path / "prior_work.txt").exists()

    asyncio.run(run())


# AC 4: merger discriminator + no continuation → replaceable opener (discards content)


def test_sandbox_entry_merger_kind_no_continuation_rebuilds_worktree(deps, git_svc):
    path = _merger_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, MERGER_BRANCH)
    _write_session(path, AgentRole.MERGER, fingerprint=FINGERPRINT)
    (path / "prior_work.txt").write_text("stale content")

    async def run():
        async with sandbox_entry(
            MergerSandboxKind(ISSUE_NUMBER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, _role_session):
            assert not (sandbox_path / "prior_work.txt").exists()

    asyncio.run(run())


# AC 4: merger discriminator yields RoleSession with merger role


def test_sandbox_entry_merger_kind_yields_merger_role(deps):
    async def run():
        async with sandbox_entry(
            MergerSandboxKind(ISSUE_NUMBER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, role_session):
            assert isinstance(role_session, RoleSession)
            assert role_session.path == sandbox_path / ".pycastle-session" / "merger"

    asyncio.run(run())


# AC 5: SetupPhaseError from guard propagates; body does not yield


def test_sandbox_entry_propagates_setup_phase_error_from_guard(deps, monkeypatch):
    def _raising_guard(**_kwargs):
        raise SetupPhaseError("test-role", "mount path outside managed worktrees dir")

    monkeypatch.setattr(
        "pycastle.iteration.sandbox_role_session.guard_managed_worktree_mount",
        _raising_guard,
    )

    did_yield = False

    async def run():
        nonlocal did_yield
        async with sandbox_entry(
            ReusableSandboxKind(SandboxWorktreeIntent.PLAN, AgentRole.PLANNER),
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as _:
            did_yield = True

    with pytest.raises(SetupPhaseError):
        asyncio.run(run())
    assert not did_yield
