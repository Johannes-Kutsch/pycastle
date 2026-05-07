"""Tests for improve_phase: improve-sandbox worktree + single-prompt smoke."""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agent_output_protocol import AgentRole, CompletionOutput
from pycastle.config import Config
from pycastle.iteration._deps import FakeAgentRunner
from pycastle.iteration.improve import IMPROVE_SANDBOX, improve_phase
from pycastle.services import GitService
from pycastle.status_display import PlainStatusDisplay


@dataclasses.dataclass
class _ImproveDepsStub:
    repo_root: Path
    git_svc: GitService
    agent_runner: FakeAgentRunner
    cfg: Config
    status_display: PlainStatusDisplay


@pytest.fixture
def git_svc(tmp_path):
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    svc.verify_ref_exists.return_value = False
    svc.get_current_branch.return_value = IMPROVE_SANDBOX
    svc.list_worktrees.return_value = []

    def _fake_create_worktree(repo, wt, branch, sha=None):
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "pyproject.toml").write_text("[project]\nname='t'\n")

    svc.create_worktree.side_effect = _fake_create_worktree
    return svc


@pytest.fixture
def agent_runner():
    return FakeAgentRunner([CompletionOutput()])


@pytest.fixture
def deps(tmp_path, git_svc, agent_runner):
    return _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=agent_runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )


def _run(deps):
    asyncio.run(improve_phase(deps))


# ── Tracer bullet: agent is called with IMPROVE role ─────────────────────────


def test_improve_phase_runs_agent_with_improve_role(deps, agent_runner):
    """improve_phase dispatches the Improve Agent with AgentRole.IMPROVE."""
    _run(deps)
    assert len(agent_runner.calls) == 1
    assert agent_runner.calls[0].role == AgentRole.IMPROVE


# ── Preflight is always skipped ───────────────────────────────────────────────


def test_improve_phase_skips_preflight(deps, agent_runner):
    """improve_phase always sets skip_preflight=True — the sandbox is not a quality gate."""
    _run(deps)
    assert agent_runner.calls[0].skip_preflight is True


# ── Worktree shape mirrors merge-sandbox ─────────────────────────────────────


def test_improve_phase_mounts_improve_sandbox_path(deps, agent_runner, tmp_path):
    """Agent is mounted at the improve-sandbox worktree path."""
    _run(deps)
    expected = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    assert agent_runner.calls[0].mount_path == expected


def test_improve_phase_creates_worktree_on_improve_sandbox_branch(deps, git_svc):
    """Worktree is created on the pycastle/improve-sandbox branch."""
    _run(deps)
    git_svc.create_worktree.assert_called_once()
    _repo, _wt, branch, _sha = git_svc.create_worktree.call_args[0]
    assert branch == IMPROVE_SANDBOX


def test_improve_phase_pins_worktree_to_head_sha(deps, git_svc):
    """Worktree is created from the HEAD SHA, not a hardcoded value."""
    git_svc.get_head_sha.return_value = "deadbeef"
    _run(deps)
    _repo, _wt, _branch, sha = git_svc.create_worktree.call_args[0]
    assert sha == "deadbeef"


# ── Prompt file ───────────────────────────────────────────────────────────────


def test_improve_phase_uses_improve_prompt_file(deps, agent_runner):
    """Agent prompt file is named improve-prompt.md."""
    _run(deps)
    assert agent_runner.calls[0].prompt_file.name == "improve-prompt.md"
