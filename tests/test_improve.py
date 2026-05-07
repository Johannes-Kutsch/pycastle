"""Tests for improve_phase: multi-prompt Work-phase, phase progress file, NO-CANDIDATE protocol."""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agent_output_protocol import (
    AgentRole,
    CompletionOutput,
    NoCandidateOutput,
)
from pycastle.config import Config
from pycastle.iteration._deps import FakeAgentRunner
from pycastle.iteration.improve import (
    IMPROVE_SANDBOX,
    _phase_id,
    _read_progress,
    improve_phase,
    next_prompt,
)
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
    # Happy path: 01-scan → 02-prd → 03-issues → terminal (3 calls)
    return FakeAgentRunner([CompletionOutput(), CompletionOutput(), CompletionOutput()])


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


# ── next_prompt: pure transition function ────────────────────────────────────


def test_next_prompt_returns_scan_on_fresh_start():
    """Fresh run (no progress) starts at 01-scan."""
    assert next_prompt(None, no_candidate_report=True) == "01-scan.md"


def test_next_prompt_returns_scan_on_fresh_start_report_disabled():
    """No-candidate-report setting does not affect fresh start."""
    assert next_prompt(None, no_candidate_report=False) == "01-scan.md"


def test_next_prompt_returns_prd_after_picked():
    """Picked candidate routes to phase 2 PRD."""
    assert next_prompt("01-scan:picked", no_candidate_report=True) == "02-prd.md"


def test_next_prompt_returns_issues_after_prd():
    """Completed PRD routes to phase 3 sub-issues."""
    assert next_prompt("02-prd", no_candidate_report=True) == "03-issues.md"


def test_next_prompt_returns_none_after_issues():
    """Completed sub-issues phase is terminal."""
    assert next_prompt("03-issues", no_candidate_report=True) is None


def test_next_prompt_returns_report_after_no_candidate_when_enabled():
    """NO-CANDIDATE with report enabled routes to phase 4 report."""
    assert (
        next_prompt("01-scan:no-candidate", no_candidate_report=True)
        == "04-no-candidate-report.md"
    )


def test_next_prompt_returns_none_after_no_candidate_when_disabled():
    """NO-CANDIDATE with report disabled is terminal."""
    assert next_prompt("01-scan:no-candidate", no_candidate_report=False) is None


def test_next_prompt_returns_none_after_report():
    """Completed no-candidate report is terminal."""
    assert next_prompt("04-report", no_candidate_report=True) is None


def test_next_prompt_returns_none_for_unknown_id():
    """Unrecognised phase ID maps to terminal (fail-soft)."""
    assert next_prompt("bogus-phase", no_candidate_report=True) is None


# ── _phase_id: output → completed phase ID ───────────────────────────────────


def test_phase_id_scan_with_completion():
    assert _phase_id("01-scan.md", CompletionOutput()) == "01-scan:picked"


def test_phase_id_scan_with_no_candidate():
    assert _phase_id("01-scan.md", NoCandidateOutput()) == "01-scan:no-candidate"


def test_phase_id_prd():
    assert _phase_id("02-prd.md", CompletionOutput()) == "02-prd"


def test_phase_id_issues():
    assert _phase_id("03-issues.md", CompletionOutput()) == "03-issues"


def test_phase_id_report():
    assert _phase_id("04-no-candidate-report.md", CompletionOutput()) == "04-report"


# ── _read_progress: phase progress file I/O ─────────────────────────────────


def test_read_progress_returns_none_for_missing_file(tmp_path):
    assert _read_progress(tmp_path / "nonexistent") is None


def test_read_progress_returns_content(tmp_path):
    f = tmp_path / "_phase_progress"
    f.write_text("01-scan:picked", encoding="utf-8")
    assert _read_progress(f) == "01-scan:picked"


def test_read_progress_trims_whitespace(tmp_path):
    f = tmp_path / "_phase_progress"
    f.write_text("  02-prd\n", encoding="utf-8")
    assert _read_progress(f) == "02-prd"


def test_read_progress_returns_none_for_empty_file(tmp_path):
    f = tmp_path / "_phase_progress"
    f.write_text("", encoding="utf-8")
    assert _read_progress(f) is None


# ── improve_phase: integration behavior ──────────────────────────────────────


def test_improve_phase_runs_agent_with_improve_role(deps, agent_runner):
    """improve_phase dispatches the Improve Agent with AgentRole.IMPROVE."""
    _run(deps)
    assert all(call.role == AgentRole.IMPROVE for call in agent_runner.calls)


def test_improve_phase_skips_preflight(deps, agent_runner):
    """improve_phase always sets skip_preflight=True."""
    _run(deps)
    assert all(call.skip_preflight is True for call in agent_runner.calls)


def test_improve_phase_mounts_improve_sandbox_path(deps, agent_runner, tmp_path):
    """Agent is mounted at the improve-sandbox worktree path."""
    _run(deps)
    expected = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    assert all(call.mount_path == expected for call in agent_runner.calls)


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


# ── Multi-prompt execution ───────────────────────────────────────────────────


def test_improve_phase_uses_scan_prompt_first(deps, agent_runner):
    """First agent call uses 01-scan.md."""
    _run(deps)
    assert agent_runner.calls[0].prompt_file.name == "01-scan.md"


def test_improve_phase_two_invocations_on_picked_path(deps, agent_runner):
    """Picked path (scan → prd) triggers two agent calls."""
    agent_runner._responses = [
        CompletionOutput(),
        CompletionOutput(),
        CompletionOutput(),
    ]
    _run(deps)
    names = [c.prompt_file.name for c in agent_runner.calls]
    assert names[:2] == ["01-scan.md", "02-prd.md"]


def test_improve_phase_two_invocations_on_no_candidate_path(tmp_path, git_svc):
    """NO-CANDIDATE path (scan → report) triggers exactly two agent calls."""
    runner = FakeAgentRunner([NoCandidateOutput(), CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert len(runner.calls) == 2
    assert runner.calls[0].prompt_file.name == "01-scan.md"
    assert runner.calls[1].prompt_file.name == "04-no-candidate-report.md"


def test_improve_phase_one_invocation_when_no_candidate_report_disabled(
    tmp_path, git_svc
):
    """NO-CANDIDATE with report disabled terminates after one call."""
    runner = FakeAgentRunner([NoCandidateOutput()])
    cfg = dataclasses.replace(Config(), improve_no_candidate_report=False)
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=cfg,
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert len(runner.calls) == 1


# ── Phase progress file writes ───────────────────────────────────────────────


def test_improve_phase_writes_progress_file_after_run(tmp_path, git_svc):
    """Phase progress file exists after a completed improve run."""
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()]
    )
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    worktree_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    progress_file = worktree_path / ".pycastle-session" / "improve" / "_phase_progress"
    assert progress_file.exists()


def test_improve_phase_progress_file_has_correct_terminal_id_on_no_candidate(
    tmp_path, git_svc
):
    """Phase progress file ends with '04-report' after NO-CANDIDATE path."""
    runner = FakeAgentRunner([NoCandidateOutput(), CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    worktree_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    progress_file = worktree_path / ".pycastle-session" / "improve" / "_phase_progress"
    assert progress_file.read_text(encoding="utf-8").strip() == "04-report"


def test_improve_phase_progress_file_written_after_scan_no_candidate(tmp_path, git_svc):
    """Phase progress file contains '01-scan:no-candidate' after first phase on NO-CANDIDATE."""
    progress_values: list[str] = []
    worktree_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    progress_file = worktree_path / ".pycastle-session" / "improve" / "_phase_progress"

    call_count = 0

    def _side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return NoCandidateOutput()
        # Read progress before second call executes
        if progress_file.exists():
            progress_values.append(progress_file.read_text(encoding="utf-8").strip())
        return CompletionOutput()

    runner = FakeAgentRunner(side_effect=_side_effect)
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert "01-scan:no-candidate" in progress_values
