"""Tests for improve_phase: multi-prompt Work-phase, phase progress file, NO-CANDIDATE protocol."""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import (
    AgentRole,
    CompletionOutput,
    IssueOutput,
    NoCandidateOutput,
)
from pycastle.config import Config, StageOverride
from pycastle.iteration._deps import FakeAgentRunner, _make_deps
from pycastle.iteration.improve import (
    IMPROVE_SANDBOX,
    ImproveContinue,
    ImproveNoCandidate,
    improve_phase,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import CodexService, GitService, ServiceRegistry
from pycastle.session import RoleSession


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
    return FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )


@pytest.fixture
def deps(tmp_path, git_svc, agent_runner):
    return _make_deps(tmp_path, agent_runner, git_svc=git_svc)


def _run(deps):
    return asyncio.run(improve_phase(deps))


# ── improve_phase: integration behavior ──────────────────────────────────────


def test_improve_phase_runs_agent_with_improve_role(deps, agent_runner):
    """improve_phase dispatches the Improve Agent with AgentRole.IMPROVE."""
    _run(deps)
    assert all(call.role == AgentRole.IMPROVE for call in agent_runner.calls)


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


def test_improve_phase_uses_improve_override_service(tmp_path, git_svc):
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=cfg)

    _run(deps)

    assert {call.service for call in runner.calls} == {"codex"}


# ── Multi-prompt execution ───────────────────────────────────────────────────


def test_improve_phase_uses_scan_prompt_first(deps, agent_runner):
    """First agent call uses IMPROVE_SCAN template."""
    _run(deps)
    assert agent_runner.calls[0].template == PromptTemplate.IMPROVE_SCAN


def test_improve_phase_picked_path_runs_scan_then_prd(deps, agent_runner):
    """Picked path runs IMPROVE_SCAN then IMPROVE_PRD in order."""
    _run(deps)
    templates = [c.template for c in agent_runner.calls]
    assert templates[:2] == [PromptTemplate.IMPROVE_SCAN, PromptTemplate.IMPROVE_PRD]


@pytest.mark.parametrize(
    "template,expected_name,expected_body",
    [
        (PromptTemplate.IMPROVE_SCAN, "Scan Agent", "picking an improvement"),
        (PromptTemplate.IMPROVE_PRD, "PRD Agent", "writing PRD"),
        (PromptTemplate.IMPROVE_ISSUES, "Slice Agent", "filing sub-issues"),
        (
            PromptTemplate.IMPROVE_NO_CANDIDATE,
            "Rejection Report Agent",
            "filing no-candidate report",
        ),
    ],
)
def test_improve_phase_dispatches_per_phase_display(
    tmp_path, git_svc, template, expected_name, expected_body
):
    """Each phase dispatches with its own RunRequest name and work_body."""
    if template == PromptTemplate.IMPROVE_NO_CANDIDATE:
        outputs = [NoCandidateOutput(), CompletionOutput()]
    else:
        outputs = [CompletionOutput(), CompletionOutput(), CompletionOutput()]
    runner = FakeAgentRunner(outputs, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    call = next(c for c in runner.calls if c.template == template)
    assert call.name == expected_name
    assert call.work_body == expected_body


def test_improve_phase_two_invocations_on_no_candidate_path(tmp_path, git_svc):
    """NO-CANDIDATE path (scan → report) triggers exactly two agent calls."""
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert len(runner.calls) == 2
    assert runner.calls[0].template == PromptTemplate.IMPROVE_SCAN
    assert runner.calls[1].template == PromptTemplate.IMPROVE_NO_CANDIDATE


def test_improve_phase_one_invocation_when_no_candidate_report_disabled(
    tmp_path, git_svc
):
    """NO-CANDIDATE with report disabled terminates after one call."""
    runner = FakeAgentRunner([NoCandidateOutput()], preflight_responses=[[]])
    cfg = dataclasses.replace(Config(), diagnose_on_failure=False)
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=cfg)
    _run(deps)
    assert len(runner.calls) == 1


# ── Phase progress file writes ───────────────────────────────────────────────


def test_improve_phase_removes_session_on_terminal_success(tmp_path, git_svc):
    """Role session dir is removed (no stage-done sentinel) after successful improve run.

    Improve-sandbox has no downstream stage that needs the sentinel, so the dir is
    removed outright to let managed_worktree's teardown predicate fire.
    """
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    worktree_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    assert not role_session_dir.exists()


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

    runner = FakeAgentRunner(side_effect=_side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert "01-scan:no-candidate" in progress_values


# ── IMPROVE_SHORT_SID prompt arg threading ───────────────────────────────────


def test_improve_phase_threads_short_sid_to_prd_phase(deps, agent_runner):
    """Phase 2 (PRD) RunRequest carries IMPROVE_SHORT_SID in scope_args."""
    _run(deps)
    prd_call = next(
        c for c in agent_runner.calls if c.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.scope_args is not None
    assert len(prd_call.scope_args.get("IMPROVE_SHORT_SID", "")) == 8


def test_improve_phase_threads_short_sid_to_issues_phase(deps, agent_runner):
    """Phase 3 (sub-issues) RunRequest carries IMPROVE_SHORT_SID in scope_args."""
    _run(deps)
    issues_call = next(
        c for c in agent_runner.calls if c.template == PromptTemplate.IMPROVE_ISSUES
    )
    assert issues_call.scope_args is not None
    assert len(issues_call.scope_args.get("IMPROVE_SHORT_SID", "")) == 8


def test_improve_phase_issues_scope_args_include_all_improve_issues_keys(
    deps, agent_runner
):
    """Phase 3 scope_args carry all IMPROVE_ISSUES placeholders."""
    _run(deps)
    issues_call = next(
        c for c in agent_runner.calls if c.template == PromptTemplate.IMPROVE_ISSUES
    )
    required = {
        "IMPROVE_SHORT_SID",
        "ISSUE_NUMBER",
        "ISSUE_TITLE",
        "ISSUE_BODY",
        "ISSUE_COMMENTS",
    }
    assert required == set(issues_call.scope_args.keys())


def test_improve_phase_threads_prd_number_from_issue_output_to_issues_phase(
    tmp_path, git_svc
):
    """Phase 02 IssueOutput.number is plumbed into phase 03's ISSUE_NUMBER scope arg."""
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"number": 4242, "title": "PRD", "body": "body"}
    github_svc.get_issue_comments.return_value = []
    runner = FakeAgentRunner(
        [
            CompletionOutput(),  # 01-scan
            IssueOutput(number=4242, labels=[]),  # 02-prd
            CompletionOutput(),  # 03-issues
        ],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)
    _run(deps)
    issues_call = next(
        c for c in runner.calls if c.template == PromptTemplate.IMPROVE_ISSUES
    )
    assert issues_call.scope_args["ISSUE_NUMBER"] == "4242"
    github_svc.get_issue.assert_called_with(4242)


def test_improve_phase_assembles_prd_title_and_body_into_issues_scope(
    tmp_path, git_svc
):
    """Phase 03 scope_args carry the PRD title and body fetched from gh_svc."""
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {
        "number": 99,
        "title": "My PRD Title",
        "body": "PRD body text",
    }
    github_svc.get_issue_comments.return_value = []
    runner = FakeAgentRunner(
        [
            CompletionOutput(),  # 01-scan
            IssueOutput(number=99, labels=[]),  # 02-prd
            CompletionOutput(),  # 03-issues
        ],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)
    _run(deps)
    issues_call = next(
        c for c in runner.calls if c.template == PromptTemplate.IMPROVE_ISSUES
    )
    assert issues_call.scope_args["ISSUE_TITLE"] == "My PRD Title"
    assert issues_call.scope_args["ISSUE_BODY"] == "PRD body text"


def test_improve_phase_fetches_prd_comments_for_issues_scope(tmp_path, git_svc):
    """improve_phase calls gh_svc.get_issue_comments with the PRD number for phase 03."""
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"number": 77, "title": "PRD", "body": ""}
    github_svc.get_issue_comments.return_value = [
        {"author": "alice", "created_at": "2026-01-01T00:00:00Z", "body": "looks good"}
    ]
    runner = FakeAgentRunner(
        [
            CompletionOutput(),  # 01-scan
            IssueOutput(number=77, labels=[]),  # 02-prd
            CompletionOutput(),  # 03-issues
        ],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)
    _run(deps)
    github_svc.get_issue_comments.assert_called_with(77)
    issues_call = next(
        c for c in runner.calls if c.template == PromptTemplate.IMPROVE_ISSUES
    )
    assert "alice" in issues_call.scope_args["ISSUE_COMMENTS"]
    assert "looks good" in issues_call.scope_args["ISSUE_COMMENTS"]


def test_improve_phase_threads_short_sid_to_no_candidate_report_phase(
    tmp_path, git_svc
):
    """Phase 4 (no-candidate report) RunRequest carries IMPROVE_SHORT_SID."""
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    report_call = runner.calls[1]
    assert report_call.scope_args is not None
    assert len(report_call.scope_args.get("IMPROVE_SHORT_SID", "")) == 8


def test_improve_phase_does_not_thread_short_sid_to_scan_phase(deps, agent_runner):
    """Phase 1 (scan) RunRequest does not receive IMPROVE_SHORT_SID."""
    _run(deps)
    scan_call = agent_runner.calls[0]
    assert (scan_call.scope_args or {}).get("IMPROVE_SHORT_SID") is None


def test_improve_phase_short_sid_is_consistent_across_phases(deps, agent_runner):
    """All phases that receive IMPROVE_SHORT_SID use the same 8-hex value."""
    _run(deps)
    sid_values = [
        c.scope_args["IMPROVE_SHORT_SID"]
        for c in agent_runner.calls
        if c.scope_args and "IMPROVE_SHORT_SID" in c.scope_args
    ]
    assert len(sid_values) == 2  # phases 02 and 03 on the picked path
    assert len(set(sid_values)) == 1  # all the same value


# ── Cross-teardown resume ─────────────────────────────────────────────────────


def _seed_progress(worktree_path: Path, phase_id: str) -> None:
    """Pre-seed the phase progress file to simulate a prior partial run."""
    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (role_session_dir / "_phase_progress").write_text(phase_id, encoding="utf-8")


def _seed_main_codex_transcript(
    worktree_path: Path,
    *,
    provider_session_id: str = "thread-phase-1",
) -> None:
    main_session = RoleSession(worktree_path, AgentRole.IMPROVE, "main")
    main_session.start_fresh()
    main_session.save_service_session_metadata("codex", provider_session_id)
    main_session.save_service_session_id("codex", provider_session_id)
    sessions_dir = main_session.path / "codex" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "rollout-1.jsonl").write_text(
        f'{{"type":"thread.started","thread_id":"{provider_session_id}"}}\n',
        encoding="utf-8",
    )


def test_improve_resumes_at_prd_after_scan_picked(tmp_path, git_svc):
    """Resume from '01-scan:picked' starts at phase 2 (PRD)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    _seed_main_codex_transcript(wt)
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        service_registry=ServiceRegistry({"codex": CodexService()}),
        cfg=Config(improve_override=StageOverride(service="codex", effort="medium")),
    )
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_PRD
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_requires_matching_resumable_main_transcript(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    _seed_main_codex_transcript(wt)
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"number": 17, "title": "PRD", "body": "body"}
    github_svc.get_issue_comments.return_value = []
    runner = FakeAgentRunner(
        [IssueOutput(number=17, labels=[]), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls[0].template == PromptTemplate.IMPROVE_PRD
    assert runner.calls[0].send_role_prompt_on_resume is True
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_without_phase_1_metadata_does_not_dispatch_prd(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    main_session = RoleSession(wt, AgentRole.IMPROVE, "main")
    main_session.start_fresh()
    sessions_dir = main_session.path / "codex" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "rollout-1.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-phase-1"}\n',
        encoding="utf-8",
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"number": 17, "title": "PRD", "body": "body"}
    github_svc.get_issue_comments.return_value = []
    runner = FakeAgentRunner(
        [IssueOutput(number=17, labels=[]), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls == []


def test_improve_handoff_failure_prints_phase_1_restart_notice(tmp_path, git_svc):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    status_display = MagicMock()
    runner = FakeAgentRunner([], preflight_responses=[[]])
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        status_display=status_display,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls == []
    status_display.print.assert_any_call(
        "Improve",
        "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
    )


def test_improve_handoff_failure_wipes_stale_improve_session_state(tmp_path, git_svc):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    main_session = RoleSession(wt, AgentRole.IMPROVE, "main")
    main_session.start_fresh()
    main_session.save_service_session_metadata("codex", "thread-phase-1")
    main_session.save_service_session_id("codex", "thread-phase-1")
    sessions_dir = main_session.path / "codex" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "rollout-1.jsonl").write_text(
        '{"type":"thread.started","thread_id":"different-thread"}\n',
        encoding="utf-8",
    )
    runner = FakeAgentRunner([], preflight_responses=[[]])
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls == []
    assert not (wt / ".pycastle-session" / "improve").exists()


def test_improve_cross_service_handoff_failure_prints_phase_1_restart_notice(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    _seed_main_codex_transcript(wt)
    status_display = MagicMock()
    runner = FakeAgentRunner([], preflight_responses=[[]])
    cfg = Config(improve_override=StageOverride(service="claude", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        status_display=status_display,
        service_registry=ServiceRegistry(
            {"claude": MagicMock(), "codex": CodexService()}
        ),
    )

    _run(deps)

    assert runner.calls == []
    status_display.print.assert_any_call(
        "Improve",
        "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
    )
    assert not (wt / ".pycastle-session" / "improve").exists()


def test_improve_clean_phase_2_entry_with_different_selected_service_does_not_dispatch_prd(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    _seed_main_codex_transcript(wt)
    runner = FakeAgentRunner(
        [IssueOutput(number=17, labels=[]), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="claude", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry(
            {"claude": MagicMock(), "codex": CodexService()}
        ),
    )

    _run(deps)

    assert runner.calls == []


def test_improve_clean_phase_2_entry_with_non_resumable_main_state_does_not_dispatch_prd(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    main_session = RoleSession(wt, AgentRole.IMPROVE, "main")
    main_session.start_fresh()
    main_session.save_service_session_metadata("codex", "thread-phase-1")
    main_session.save_service_session_id("codex", "thread-phase-1")
    runner = FakeAgentRunner(
        [IssueOutput(number=17, labels=[]), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls == []


def test_improve_clean_phase_2_entry_with_conflicting_main_session_state_does_not_dispatch_prd(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    _seed_main_codex_transcript(wt, provider_session_id="thread-from-rollout")
    main_session = RoleSession(wt, AgentRole.IMPROVE, "main")
    main_session.save_service_session_metadata("codex", "thread-from-metadata")
    runner = FakeAgentRunner(
        [IssueOutput(number=17, labels=[]), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls == []


def test_improve_clean_phase_2_entry_with_conflicting_codex_rollout_state_does_not_dispatch_prd(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    _seed_main_codex_transcript(wt, provider_session_id="thread-from-sidecar")
    sessions_dir = wt / ".pycastle-session" / "improve" / "main" / "codex" / "sessions"
    (sessions_dir / "rollout-1.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"number": 17, "title": "PRD", "body": "body"}
    github_svc.get_issue_comments.return_value = []
    runner = FakeAgentRunner(
        [IssueOutput(number=17, labels=[]), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls == []


def test_improve_gate_failure_restarts_next_entry_from_scan_phase(tmp_path, git_svc):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    runner = FakeAgentRunner([], preflight_responses=[[]])
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert runner.calls == []

    follow_up = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )
    follow_up_deps = _make_deps(
        tmp_path,
        follow_up,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(follow_up_deps)

    assert follow_up.calls[0].template == PromptTemplate.IMPROVE_SCAN


def test_improve_resumes_at_report_after_scan_no_candidate(tmp_path, git_svc):
    """Resume from '01-scan:no-candidate' starts at phase 4 (report)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:no-candidate")
    runner = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_NO_CANDIDATE
    assert len(runner.calls) == 1


def test_improve_orphan_reset_when_prd_done_but_no_in_flight(tmp_path, git_svc):
    """Progress='02-prd' without in-flight='03-issues' means prd_number was lost.
    improve_phase clears progress and restarts from phase 1 (orphan-reset)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "02-prd")
    # 3 responses: scan → prd → issues (full fresh cycle)
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_SCAN
    assert len(runner.calls) == 3


def test_improve_resumes_at_issues_mid_phase(tmp_path, git_svc):
    """Progress='02-prd' WITH in-flight='03-issues' means phase 3 was in flight.
    improve_phase resumes at phase 3 (no orphan-reset)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = wt / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (role_session_dir / "_phase_progress").write_text("02-prd", encoding="utf-8")
    (role_session_dir / "_phase_in_flight").write_text("03-issues", encoding="utf-8")
    runner = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_ISSUES
    assert len(runner.calls) == 1


def test_improve_resumes_mid_phase_2_without_clean_entry_gate(tmp_path, git_svc):
    """Progress='02-prd' WITH in-flight='02-prd' resumes phase 2 as a continuation."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = wt / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (role_session_dir / "_phase_progress").write_text("02-prd", encoding="utf-8")
    (role_session_dir / "_phase_in_flight").write_text("02-prd", encoding="utf-8")
    github_svc = MagicMock()
    github_svc.get_issue.return_value = {"number": 17, "title": "PRD", "body": "body"}
    github_svc.get_issue_comments.return_value = []
    runner = FakeAgentRunner(
        [IssueOutput(number=17, labels=[]), CompletionOutput()],
        preflight_responses=[[]],
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        github_svc=github_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls[0].template == PromptTemplate.IMPROVE_PRD
    assert runner.calls[0].send_role_prompt_on_resume is False
    assert len(runner.calls) == 2


def test_improve_is_terminal_after_issues(tmp_path, git_svc):
    """Resume from '03-issues' is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "03-issues")
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert len(runner.calls) == 0


def test_improve_is_terminal_after_report(tmp_path, git_svc):
    """Resume from '04-report' is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "04-report")
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
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
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    prd_call = next(c for c in runner.calls if c.template == PromptTemplate.IMPROVE_PRD)
    assert prd_call.send_role_prompt_on_resume is False


def test_cross_teardown_resume_at_phase_2_signals_role_prompt(tmp_path, git_svc):
    """Resume from '01-scan:picked' (phase 1 completed, container torn down):
    phase 2's RunRequest signals send_role_prompt_on_resume=True so the PRD
    prompt is delivered, not the continuation prompt."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "01-scan:picked")
    _seed_main_codex_transcript(wt)
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        service_registry=ServiceRegistry({"codex": CodexService()}),
        cfg=Config(improve_override=StageOverride(service="codex", effort="medium")),
    )
    _run(deps)
    prd_call = next(c for c in runner.calls if c.template == PromptTemplate.IMPROVE_PRD)
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
    prd_call = next(
        c for c in agent_runner.calls if c.template == PromptTemplate.IMPROVE_PRD
    )
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
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_SCAN


def test_improve_fresh_run_on_empty_progress_file(tmp_path, git_svc):
    """Empty progress file is treated as missing — fresh run starting at phase 1."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "")
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_SCAN


def test_improve_fresh_run_on_whitespace_only_progress_file(tmp_path, git_svc):
    """Whitespace-only progress file is treated as missing — fresh run starting at phase 1."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "\n  \t  \n")
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_SCAN


def test_improve_resumes_correctly_with_whitespace_padded_progress(tmp_path, git_svc):
    """Progress file with a valid phase ID surrounded by whitespace is still recognized — resumes at correct phase."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_progress(wt, "  01-scan:picked  \n")
    _seed_main_codex_transcript(wt)
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        service_registry=ServiceRegistry({"codex": CodexService()}),
        cfg=Config(improve_override=StageOverride(service="codex", effort="medium")),
    )
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_PRD
    assert len(runner.calls) == 2


# ── Session namespace per phase ───────────────────────────────────────────────


def test_improve_phases_01_02_04_use_main_namespace(tmp_path, git_svc):
    """Phases 01-scan, 02-prd, and 04-no-candidate-report must use session_namespace='main'."""
    no_candidate_cfg = Config(logs_dir=tmp_path, diagnose_on_failure=True)
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()],  # 01-scan NO-CANDIDATE → 04-report
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=no_candidate_cfg)
    _run(deps)
    assert runner.calls[0].template == PromptTemplate.IMPROVE_SCAN
    assert runner.calls[0].session_namespace == "main"
    assert runner.calls[1].template == PromptTemplate.IMPROVE_NO_CANDIDATE
    assert runner.calls[1].session_namespace == "main"


def test_improve_phase_02_uses_main_namespace(deps, agent_runner):
    """Phase 02-prd must use session_namespace='main'."""
    _run(deps)
    prd_call = next(
        c for c in agent_runner.calls if c.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.session_namespace == "main"


def test_improve_phase_03_uses_issues_namespace(deps, agent_runner):
    """Phase 03-issues must use session_namespace='issues' for an isolated Claude session."""
    _run(deps)
    issues_call = next(
        c for c in agent_runner.calls if c.template == PromptTemplate.IMPROVE_ISSUES
    )
    assert issues_call.session_namespace == "issues"


def test_improve_all_phases_have_correct_namespace(deps, agent_runner):
    """Happy path: namespaces across all three phases match the expected mapping."""
    _run(deps)
    assert agent_runner.calls[0].template == PromptTemplate.IMPROVE_SCAN
    assert agent_runner.calls[0].session_namespace == "main"
    assert agent_runner.calls[1].template == PromptTemplate.IMPROVE_PRD
    assert agent_runner.calls[1].session_namespace == "main"
    assert agent_runner.calls[2].template == PromptTemplate.IMPROVE_ISSUES
    assert agent_runner.calls[2].session_namespace == "issues"


# ── Return type: sum-type variants ───────────────────────────────────────────


def test_improve_phase_returns_improve_continue_on_picked_path(deps):
    """Happy path (candidate found and filed) returns ImproveContinue."""
    result = _run(deps)
    assert isinstance(result, ImproveContinue)


def test_improve_phase_returns_improve_no_candidate_on_no_candidate_path(
    tmp_path, git_svc
):
    """NO-CANDIDATE path returns ImproveNoCandidate."""
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    result = _run(deps)
    assert isinstance(result, ImproveNoCandidate)


def test_improve_phase_returns_improve_no_candidate_when_report_disabled(
    tmp_path, git_svc
):
    """NO-CANDIDATE with report disabled (scan terminates) returns ImproveNoCandidate."""
    runner = FakeAgentRunner([NoCandidateOutput()], preflight_responses=[[]])
    cfg = dataclasses.replace(Config(), diagnose_on_failure=False)
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=cfg)
    result = _run(deps)
    assert isinstance(result, ImproveNoCandidate)
