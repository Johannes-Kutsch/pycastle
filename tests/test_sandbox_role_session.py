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
    merger_sandbox_entry,
    reusable_sandbox_entry,
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


# ── Behavior 1: fingerprint match → reusable entry yields resumable session ───


def test_reusable_sandbox_entry_fingerprint_match_yields_resumable_session(
    deps, git_svc
):
    path = _plan_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, PLAN_BRANCH)
    _write_session(
        path, AgentRole.PLANNER, fingerprint=FINGERPRINT, with_continuation=True
    )
    git_svc.get_current_branch.return_value = PLAN_BRANCH

    async def run():
        async with reusable_sandbox_entry(
            SandboxWorktreeIntent.PLAN,
            fingerprint=FINGERPRINT,
            role=AgentRole.PLANNER,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (_sandbox_path, role_session):
            assert role_session.is_resumable()

    asyncio.run(run())


# ── Behavior 2: fingerprint mismatch → reusable entry yields non-resumable session ─


def test_reusable_sandbox_entry_fingerprint_mismatch_yields_discarded_session(
    deps, git_svc
):
    path = _plan_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, PLAN_BRANCH)
    _write_session(
        path, AgentRole.PLANNER, fingerprint=OLD_FINGERPRINT, with_continuation=True
    )

    async def run():
        async with reusable_sandbox_entry(
            SandboxWorktreeIntent.PLAN,
            fingerprint=FINGERPRINT,
            role=AgentRole.PLANNER,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (_sandbox_path, role_session):
            assert not role_session.is_resumable()

    asyncio.run(run())


# ── Behavior 3: fingerprint is written inside both entry points ────────────────


def test_reusable_sandbox_entry_writes_fingerprint_to_session_file(deps):
    async def run():
        async with reusable_sandbox_entry(
            SandboxWorktreeIntent.PLAN,
            fingerprint=FINGERPRINT,
            role=AgentRole.PLANNER,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (_sandbox_path, role_session):
            assert role_session.read_fingerprint() == FINGERPRINT

    asyncio.run(run())


def test_merger_sandbox_entry_writes_fingerprint_to_session_file(deps):
    async def run():
        async with merger_sandbox_entry(
            ISSUE_NUMBER,
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (_sandbox_path, role_session):
            assert role_session.read_fingerprint() == FINGERPRINT

    asyncio.run(run())


# ── Behavior 4: guard raises SetupPhaseError before yield for outside paths ───


def test_reusable_sandbox_entry_propagates_setup_phase_error_from_guard(
    deps, monkeypatch
):
    def _raising_guard(**_kwargs):
        raise SetupPhaseError("test-role", "mount path outside managed worktrees dir")

    monkeypatch.setattr(
        "pycastle.iteration.sandbox_role_session.guard_managed_worktree_mount",
        _raising_guard,
    )

    did_yield = False

    async def run():
        nonlocal did_yield
        async with reusable_sandbox_entry(
            SandboxWorktreeIntent.PLAN,
            fingerprint=FINGERPRINT,
            role=AgentRole.PLANNER,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as _:
            did_yield = True

    with pytest.raises(SetupPhaseError):
        asyncio.run(run())
    assert not did_yield


def test_merger_sandbox_entry_propagates_setup_phase_error_from_guard(
    deps, monkeypatch
):
    def _raising_guard(**_kwargs):
        raise SetupPhaseError("merger", "mount path outside managed worktrees dir")

    monkeypatch.setattr(
        "pycastle.iteration.sandbox_role_session.guard_managed_worktree_mount",
        _raising_guard,
    )

    did_yield = False

    async def run():
        nonlocal did_yield
        async with merger_sandbox_entry(
            ISSUE_NUMBER,
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as _:
            did_yield = True

    with pytest.raises(SetupPhaseError):
        asyncio.run(run())
    assert not did_yield


# ── Behavior 5: merger entry with match + continuation → reusable opener ──────


def test_merger_sandbox_entry_fingerprint_match_with_continuation_preserves_worktree(
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
        async with merger_sandbox_entry(
            ISSUE_NUMBER,
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, _role_session):
            assert (sandbox_path / "prior_work.txt").exists()

    asyncio.run(run())


# ── Behavior 6: merger entry without resumable session → replaceable opener ───


def test_merger_sandbox_entry_fingerprint_mismatch_rebuilds_worktree(deps, git_svc):
    path = _merger_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, MERGER_BRANCH)
    _write_session(
        path, AgentRole.MERGER, fingerprint=OLD_FINGERPRINT, with_continuation=True
    )
    (path / "prior_work.txt").write_text("stale content")

    async def run():
        async with merger_sandbox_entry(
            ISSUE_NUMBER,
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, _role_session):
            assert not (sandbox_path / "prior_work.txt").exists()

    asyncio.run(run())


def test_merger_sandbox_entry_no_continuation_rebuilds_worktree(deps, git_svc):
    path = _merger_sandbox_path(deps)
    _seed_worktree(git_svc, deps.repo_root, path, MERGER_BRANCH)
    _write_session(path, AgentRole.MERGER, fingerprint=FINGERPRINT)
    (path / "prior_work.txt").write_text("stale content")

    async def run():
        async with merger_sandbox_entry(
            ISSUE_NUMBER,
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, _role_session):
            assert not (sandbox_path / "prior_work.txt").exists()

    asyncio.run(run())


# ── Behavior 7: yielded role_session is RoleSession at sandbox_path for given role ─


def test_reusable_sandbox_entry_yields_role_session_for_given_role(deps):
    async def run():
        async with reusable_sandbox_entry(
            SandboxWorktreeIntent.PLAN,
            fingerprint=FINGERPRINT,
            role=AgentRole.PLANNER,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, role_session):
            assert isinstance(role_session, RoleSession)
            assert role_session.path == sandbox_path / ".pycastle-session" / "planner"

    asyncio.run(run())


def test_merger_sandbox_entry_yields_merger_role_session(deps):
    async def run():
        async with merger_sandbox_entry(
            ISSUE_NUMBER,
            fingerprint=FINGERPRINT,
            deps=deps,
            sha=None,
            operating_branch=OPERATING_BRANCH,
        ) as (sandbox_path, role_session):
            assert isinstance(role_session, RoleSession)
            assert role_session.path == sandbox_path / ".pycastle-session" / "merger"

    asyncio.run(run())
