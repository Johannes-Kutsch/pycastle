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
from pycastle.session_resume import is_stage_done
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


def test_improve_phase_picked_path_runs_scan_then_prd(deps, agent_runner):
    """Picked path runs 01-scan then 02-prd in order."""
    _run(deps)
    names = [c.prompt_file.name for c in agent_runner.calls]
    assert names[:2] == ["01-scan.md", "02-prd.md"]


@pytest.mark.parametrize(
    "prompt_name,expected_name,expected_body",
    [
        ("01-scan.md", "Scan Agent", "picking an improvement"),
        ("02-prd.md", "PRD Agent", "writing PRD"),
        ("03-issues.md", "Slice Agent", "filing sub-issues"),
        (
            "04-no-candidate-report.md",
            "Rejection Report Agent",
            "filing no-candidate report",
        ),
    ],
)
def test_improve_phase_dispatches_per_phase_display(
    tmp_path, git_svc, prompt_name, expected_name, expected_body
):
    """Each phase dispatches with its own RunRequest name and work_body."""
    if prompt_name == "04-no-candidate-report.md":
        outputs = [NoCandidateOutput(), CompletionOutput()]
    else:
        outputs = [CompletionOutput(), CompletionOutput(), CompletionOutput()]
    runner = FakeAgentRunner(outputs)
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    call = next(c for c in runner.calls if c.prompt_file.name == prompt_name)
    assert call.name == expected_name
    assert call.work_body == expected_body


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


def test_improve_phase_clears_session_on_terminal_success(tmp_path, git_svc):
    """Role session dir is cleared (stage-done signal) after successful improve run."""
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
    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    assert is_stage_done(role_session_dir)


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


# ── IMPROVE_SHORT_SID prompt arg threading ───────────────────────────────────


def test_improve_phase_threads_short_sid_to_prd_phase(deps, agent_runner):
    """Phase 2 (PRD) RunRequest carries IMPROVE_SHORT_SID in prompt_args."""
    _run(deps)
    prd_call = next(c for c in agent_runner.calls if c.prompt_file.name == "02-prd.md")
    assert prd_call.prompt_args is not None
    assert len(prd_call.prompt_args.get("IMPROVE_SHORT_SID", "")) == 8


def test_improve_phase_threads_short_sid_to_issues_phase(deps, agent_runner):
    """Phase 3 (sub-issues) RunRequest carries IMPROVE_SHORT_SID in prompt_args."""
    _run(deps)
    issues_call = next(
        c for c in agent_runner.calls if c.prompt_file.name == "03-issues.md"
    )
    assert issues_call.prompt_args is not None
    assert len(issues_call.prompt_args.get("IMPROVE_SHORT_SID", "")) == 8


def test_improve_phase_threads_short_sid_to_no_candidate_report_phase(
    tmp_path, git_svc
):
    """Phase 4 (no-candidate report) RunRequest carries IMPROVE_SHORT_SID."""
    runner = FakeAgentRunner([NoCandidateOutput(), CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    report_call = runner.calls[1]
    assert report_call.prompt_args is not None
    assert len(report_call.prompt_args.get("IMPROVE_SHORT_SID", "")) == 8


def test_improve_phase_does_not_thread_short_sid_to_scan_phase(deps, agent_runner):
    """Phase 1 (scan) RunRequest does not receive IMPROVE_SHORT_SID."""
    _run(deps)
    scan_call = agent_runner.calls[0]
    assert (scan_call.prompt_args or {}).get("IMPROVE_SHORT_SID") is None


def test_improve_phase_short_sid_is_consistent_across_phases(deps, agent_runner):
    """All phases that receive IMPROVE_SHORT_SID use the same 8-hex value."""
    _run(deps)
    sid_values = [
        c.prompt_args["IMPROVE_SHORT_SID"]
        for c in agent_runner.calls
        if c.prompt_args and "IMPROVE_SHORT_SID" in c.prompt_args
    ]
    assert len(sid_values) == 2  # phases 02 and 03 on the picked path
    assert len(set(sid_values)) == 1  # all the same value


# ── Coding standards threading to phase 1 ───────────────────────────────────

_STANDARDS_KEYS = {
    "TESTING_STANDARDS",
    "MOCKING_STANDARDS",
    "INTERFACES_STANDARDS",
    "DEEP_MODULES_STANDARDS",
    "REFACTORING_STANDARDS",
}


def test_improve_phase_threads_coding_standards_to_scan_phase(deps, agent_runner):
    """Phase 1 (scan) RunRequest contains all coding-standards keys."""
    _run(deps)
    scan_call = agent_runner.calls[0]
    assert scan_call.prompt_args is not None
    assert _STANDARDS_KEYS <= scan_call.prompt_args.keys()


def test_improve_phase_does_not_thread_coding_standards_to_prd_phase(
    deps, agent_runner
):
    """Phase 2 (PRD) RunRequest does not contain coding-standards keys."""
    _run(deps)
    prd_call = next(c for c in agent_runner.calls if c.prompt_file.name == "02-prd.md")
    assert not _STANDARDS_KEYS & (prd_call.prompt_args or {}).keys()


def test_improve_phase_scan_standards_and_sid_are_separate(deps, agent_runner):
    """Phase 1 receives coding standards, phases 2/3 receive IMPROVE_SHORT_SID — never mixed."""
    _run(deps)
    scan_call = agent_runner.calls[0]
    assert "IMPROVE_SHORT_SID" not in (scan_call.prompt_args or {})
    for call in agent_runner.calls[1:]:
        assert not _STANDARDS_KEYS & (call.prompt_args or {}).keys()


# ── Cross-teardown resume ─────────────────────────────────────────────────────


def _seed_progress(worktree_path: Path, phase_id: str) -> None:
    """Pre-seed the phase progress file to simulate a prior partial run."""
    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (role_session_dir / "_phase_progress").write_text(phase_id, encoding="utf-8")


def test_improve_resumes_at_prd_after_scan_picked(tmp_path, git_svc):
    """Resume from '01-scan:picked' starts at phase 2 (PRD)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    runner = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert runner.calls[0].prompt_file.name == "02-prd.md"
    assert len(runner.calls) == 2


def test_improve_resumes_at_report_after_scan_no_candidate(tmp_path, git_svc):
    """Resume from '01-scan:no-candidate' starts at phase 4 (report)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:no-candidate")
    runner = FakeAgentRunner([CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert runner.calls[0].prompt_file.name == "04-no-candidate-report.md"
    assert len(runner.calls) == 1


def test_improve_resumes_at_issues_after_prd(tmp_path, git_svc):
    """Resume from '02-prd' starts at phase 3 (sub-issues)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "02-prd")
    runner = FakeAgentRunner([CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert runner.calls[0].prompt_file.name == "03-issues.md"
    assert len(runner.calls) == 1


def test_improve_is_terminal_after_issues(tmp_path, git_svc):
    """Resume from '03-issues' is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "03-issues")
    runner = FakeAgentRunner([])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert len(runner.calls) == 0


def test_improve_is_terminal_after_report(tmp_path, git_svc):
    """Resume from '04-report' is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "04-report")
    runner = FakeAgentRunner([])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    assert len(runner.calls) == 0


# ── Issue #528: phase-boundary prompt shape ──────────────────────────────────


def test_mid_phase_2_retry_does_not_signal_role_prompt(tmp_path, git_svc):
    """Resume mid-phase-2 (interrupted before COMPLETE): phase 2's role prompt
    is already in the resumed claude conversation history, so the retry must
    NOT re-send it — send_role_prompt_on_resume stays False so container_runner
    falls back to the continuation prompt."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = wt / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (role_session_dir / "_phase_progress").write_text(
        "01-scan:picked", encoding="utf-8"
    )
    (role_session_dir / "_phase_in_flight").write_text("02-prd", encoding="utf-8")
    runner = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    prd_call = next(c for c in runner.calls if c.prompt_file.name == "02-prd.md")
    assert prd_call.send_role_prompt_on_resume is False


def test_cross_teardown_resume_at_phase_2_signals_role_prompt(tmp_path, git_svc):
    """Resume from '01-scan:picked' (phase 1 completed, container torn down):
    phase 2's RunRequest signals send_role_prompt_on_resume=True so the PRD
    prompt is delivered, not the continuation prompt."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    runner = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    deps = _ImproveDepsStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        agent_runner=runner,
        cfg=Config(),
        status_display=PlainStatusDisplay(),
    )
    _run(deps)
    prd_call = next(c for c in runner.calls if c.prompt_file.name == "02-prd.md")
    assert prd_call.send_role_prompt_on_resume is True


def test_cold_start_phase_1_does_not_signal_role_prompt_on_resume(deps, agent_runner):
    """Cold start: phase 1 RunRequest leaves send_role_prompt_on_resume False
    so today's Fresh-run prompt-shape stays identical."""
    _run(deps)
    scan_call = agent_runner.calls[0]
    assert scan_call.send_role_prompt_on_resume is False


def test_phase_2_signals_role_prompt_on_resumed_session(deps, agent_runner):
    """After phase 1 completes cleanly, phase 2's RunRequest signals that the
    new role prompt must be sent despite the resumed claude session — otherwise
    the agent would receive only the continuation prompt (issue #528)."""
    _run(deps)
    prd_call = next(c for c in agent_runner.calls if c.prompt_file.name == "02-prd.md")
    assert prd_call.send_role_prompt_on_resume is True


def test_improve_fresh_run_on_malformed_progress(tmp_path, git_svc):
    """Malformed progress file falls back to a fresh run starting at phase 1 (scan)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = wt / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (role_session_dir / "_phase_progress").write_text(
        "corrupted-data", encoding="utf-8"
    )
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
    assert runner.calls[0].prompt_file.name == "01-scan.md"
