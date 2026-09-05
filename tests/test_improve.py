"""Tests for improve_phase: multi-prompt Work-phase, phase progress file, NO-CANDIDATE protocol."""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    CompletionOutput,
    NoCandidateOutput,
    ScanCandidateItem,
    ScanCandidatesOutput,
)
from pycastle.config import Config, StageOverride
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.iteration.improve import (
    IMPROVE_SANDBOX,
    ImproveContinue,
    ImproveNoCandidate,
    improve_phase,
)
from pycastle.iteration.improve_role_session_store import (
    ImproveRoleSessionStore,
)
from pycastle.prompts.dispatch import PromptKind
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.runtime_session import session_uuid as runtime_session_uuid
from pycastle.services import GithubNetworkError, ServiceRegistry
from pycastle.services.runtime_services import CodexService, OpenCodeService
from pycastle.session import RoleSession
from pycastle.session.service_session_store import (
    ServiceSessionStore,
)
from tests.support import (
    FakeAgentRunner,
    RecordingStatusDisplay,
    _draft_dir,
    _make_deps,
    _make_filing_github_svc,
    _overwrite_candidate_cursor_raw,
    _seed_candidate_list,
    _seed_candidate_record,
    _write_malformed_candidate_list,
    _write_slice_draft,
    _write_spec_draft,
    functional_git_svc,
    make_scan_output,
)


@pytest.fixture
def git_svc(tmp_path):
    svc = functional_git_svc()
    svc.get_head_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    svc.get_current_branch.return_value = IMPROVE_SANDBOX
    svc.list_worktrees.side_effect = None
    svc.list_worktrees.return_value = []
    svc.remove_worktree.side_effect = None
    return svc


@pytest.fixture
def agent_runner():
    # Happy path: 01-scan → 02-prd → 03-issues → terminal (3 calls)
    # The issues phase side-effect writes valid draft files so host filing succeeds.
    responses = [make_scan_output(), CompletionOutput(), CompletionOutput()]
    idx = [0]

    def _side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            _write_spec_draft(
                _draft_dir(RoleSession(request.mount_path, AgentRole.IMPROVE).path)
            )
        result = responses[idx[0]]
        idx[0] += 1
        return result

    return FakeAgentRunner(side_effect=_side_effect, preflight_responses=[[]])


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
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=cfg)

    _run(deps)

    assert {call.service for call in runner.calls} == {"codex"}


# ── Multi-prompt execution ───────────────────────────────────────────────────


def test_improve_phase_uses_scan_prompt_first(deps, agent_runner):
    """First agent call uses IMPROVE_SCAN template."""
    _run(deps)
    assert agent_runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN


def test_improve_phase_picked_path_runs_scan_then_prd(deps, agent_runner):
    """Picked path runs IMPROVE_SCAN then IMPROVE_SPEC in order."""
    _run(deps)
    templates = [c.prompt.template for c in agent_runner.calls]
    assert templates[:2] == [PromptTemplate.IMPROVE_SCAN, PromptTemplate.IMPROVE_SPEC]


@pytest.mark.parametrize(
    ("template", "expected_name", "expected_body"),
    [
        (PromptTemplate.IMPROVE_SCAN, "Scan Agent", "picking up to 3 improvements"),
        (
            PromptTemplate.IMPROVE_SPEC,
            "Spec Agent",
            'writing spec for candidate 1/1 "Candidate"',
        ),
        (
            PromptTemplate.IMPROVE_TICKETS,
            "Tickets Agent",
            'filing tickets for candidate 1/1 "Candidate"',
        ),
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
        runner = FakeAgentRunner(
            [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
        )
    else:
        runner = _make_runner_with_drafts(
            make_scan_output(), CompletionOutput(), CompletionOutput()
        )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    call = next(c for c in runner.calls if c.prompt.template == template)
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
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN
    assert runner.calls[1].prompt.template == PromptTemplate.IMPROVE_NO_CANDIDATE


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
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    worktree_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    assert not RoleSession(worktree_path, AgentRole.IMPROVE).path.exists()


def test_improve_phase_candidate_list_written_with_no_candidate_flag_after_scan(
    tmp_path, git_svc
):
    """After NO-CANDIDATE scan, candidate list is written with no_candidate=True."""
    candidate_list_values: list[bool] = []
    worktree_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"

    call_count = 0

    def _side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return NoCandidateOutput()
        # Read candidate list before second call executes
        candidate_list = ImproveRoleSessionStore(
            RoleSession(worktree_path, AgentRole.IMPROVE).path
        ).read_candidate_list()
        if candidate_list is not None:
            candidate_list_values.append(candidate_list.no_candidate)
        return CompletionOutput()

    runner = FakeAgentRunner(side_effect=_side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert True in candidate_list_values


# ── IMPROVE_SHORT_SID prompt arg threading ───────────────────────────────────


def test_improve_phase_dispatches_scan_prd_and_issues_templates_in_order(
    deps, agent_runner
):
    _run(deps)
    assert [(call.role, call.prompt.template) for call in agent_runner.calls] == [
        (AgentRole.IMPROVE, PromptTemplate.IMPROVE_SCAN),
        (AgentRole.IMPROVE, PromptTemplate.IMPROVE_SPEC),
        (AgentRole.IMPROVE, PromptTemplate.IMPROVE_TICKETS),
    ]


def test_improve_phase_dispatches_prd_step_with_expected_work_body(tmp_path, git_svc):
    github_svc = MagicMock()
    github_svc.get_recent_improve_specs.return_value = [
        {"number": 12, "state": "OPEN", "title": "First candidate"},
        {"number": 11, "state": "CLOSED", "title": "Second candidate"},
    ]
    github_svc.create_issue_in.return_value = (0, 0)
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    prd_call = next(
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_SPEC
    )
    assert prd_call.work_body == 'writing spec for candidate 1/1 "Candidate"'


def test_improve_phase_still_dispatches_prd_step_when_recent_prd_history_is_empty(
    tmp_path, git_svc
):
    github_svc = MagicMock()
    github_svc.get_recent_improve_specs.return_value = []
    github_svc.create_issue_in.return_value = (0, 0)
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    prd_call = next(
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_SPEC
    )
    assert prd_call.prompt.template == PromptTemplate.IMPROVE_SPEC


def test_improve_phase_propagates_recent_improve_prd_lookup_failures(tmp_path, git_svc):
    github_svc = MagicMock()
    github_svc.get_recent_improve_specs.side_effect = GithubNetworkError(
        "transport error", cause=RuntimeError("boom")
    )
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    with pytest.raises(GithubNetworkError):
        _run(deps)

    assert runner.calls == []


def test_improve_phase_propagates_prd_preparation_lookup_failures_after_scan(
    tmp_path, git_svc
):
    github_svc = MagicMock()
    github_svc.get_recent_improve_specs.side_effect = [
        [{"number": 12, "state": "OPEN", "title": "First candidate"}],
        GithubNetworkError("transport error", cause=RuntimeError("boom")),
    ]
    runner = FakeAgentRunner([make_scan_output()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    with pytest.raises(GithubNetworkError):
        _run(deps)

    assert [call.prompt.template for call in runner.calls] == [
        PromptTemplate.IMPROVE_SCAN
    ]


def test_improve_phase_dispatches_no_candidate_report_after_scan_rejection(
    tmp_path, git_svc
):
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    report_call = runner.calls[1]
    assert report_call.role == AgentRole.IMPROVE
    assert report_call.prompt.template == PromptTemplate.IMPROVE_NO_CANDIDATE


def test_improve_phase_propagates_no_candidate_report_preparation_lookup_failures(
    tmp_path, git_svc
):
    github_svc = MagicMock()
    github_svc.get_recent_improve_specs.side_effect = [
        [{"number": 12, "state": "OPEN", "title": "First candidate"}],
        GithubNetworkError("transport error", cause=RuntimeError("boom")),
    ]
    runner = FakeAgentRunner([NoCandidateOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    with pytest.raises(GithubNetworkError):
        _run(deps)

    assert [call.prompt.template for call in runner.calls] == [
        PromptTemplate.IMPROVE_SCAN
    ]


# ── Cross-teardown resume ─────────────────────────────────────────────────────

_DEFAULT_CANDIDATE = ScanCandidateItem(rank=1, title="Seeded candidate")


def _seed_exact_phase_1_main_transcript(
    worktree_path: Path,
    *,
    service_name: str,
    provider_session_id: str,
    namespace: str = "main",
) -> None:
    role_session = RoleSession(worktree_path, AgentRole.IMPROVE, namespace)
    if service_name == "opencode":
        state_dir = worktree_path / "opencode"
    elif service_name == "codex":
        state_dir = role_session.path / service_name
    else:
        state_dir = role_session.path / service_name
    state_dir.mkdir(parents=True, exist_ok=True)
    ServiceSessionStore(role_session.path).save_service_session_id(
        service_name, provider_session_id
    )
    sidecar_name = "session_id" if service_name == "opencode" else "thread_id"
    (state_dir / sidecar_name).write_text(provider_session_id, encoding="utf-8")
    if service_name == "codex":
        rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        (rollout_dir / "rollout-001.jsonl").write_text(
            f'{{"type":"thread.started","thread_id":"{provider_session_id}"}}\n',
            encoding="utf-8",
        )
    if service_name == "claude":
        (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    role_session.write_continuation(provider_session_id)


def test_improve_resumes_at_prd_after_scan_picked(tmp_path, git_svc):
    """Resume with candidate list (scan done, cursor=0, no record) starts at PRD."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="opencode",
        provider_session_id="sess-opencode-123",
        namespace="candidate/0",
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=Config(improve_override=StageOverride(service="opencode", effort="medium")),
        service_registry=ServiceRegistry({"opencode": OpenCodeService()}),
    )
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_dispatches_prd_prompt_for_exact_codex_transcript(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="codex",
        provider_session_id="thread-exact",
        namespace="candidate/0",
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert runner.calls[0].prompt.kind is PromptKind.FOLLOW_UP
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_accepts_recovered_exact_codex_transcript(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="codex",
        provider_session_id="thread-exact",
        namespace="candidate/0",
    )
    RoleSession(wt, AgentRole.IMPROVE, "candidate/0").path.joinpath(
        "codex", "thread_id"
    ).unlink()
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert runner.calls[0].prompt.kind is PromptKind.FOLLOW_UP
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_restarts_when_codex_rollout_thread_is_not_exact(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="codex",
        provider_session_id="thread-recorded",
    )
    rollout_path = RoleSession(wt, AgentRole.IMPROVE, "main").path.joinpath(
        "codex", "sessions", "2026", "05", "30", "rollout-001.jsonl"
    )
    rollout_path.write_text(
        '{"type":"thread.started","thread_id":"thread-other"}\n',
        encoding="utf-8",
    )
    status_display = MagicMock()
    runner = FakeAgentRunner([], preflight_responses=[[]])
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        status_display=status_display,
        cfg=cfg,
        service_registry=ServiceRegistry({"codex": CodexService()}),
    )

    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert runner.calls == []
    status_display.print.assert_any_call(
        "Improve",
        "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
    )
    assert not RoleSession(wt, AgentRole.IMPROVE).path.exists()


def test_improve_gate_failure_restarts_next_entry_from_scan_phase(tmp_path, git_svc):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)

    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert runner.calls == []

    follow_up = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    follow_up_deps = _make_deps(tmp_path, follow_up, git_svc=git_svc)

    _run(follow_up_deps)

    assert follow_up.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN


def test_improve_clean_phase_2_entry_restarts_from_phase_1_on_selected_service_mismatch(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="claude",
        provider_session_id=runtime_session_uuid(wt, AgentRole.IMPROVE.value, "main"),
    )
    status_display = MagicMock()
    runner = FakeAgentRunner([], preflight_responses=[[]])
    cfg = Config(improve_override=StageOverride(service="opencode", effort="medium"))
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        status_display=status_display,
        cfg=cfg,
        service_registry=ServiceRegistry({"opencode": OpenCodeService()}),
    )

    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert runner.calls == []
    status_display.print.assert_any_call(
        "Improve",
        "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
    )
    assert not RoleSession(wt, AgentRole.IMPROVE).path.exists()


# ── Issue #2241: Phase-1→2 gate rebased on continuation presence ──────────────


def test_spec_phase_dispatches_when_candidate_namespace_has_continuation(
    tmp_path, git_svc
):
    """AC1: candidate namespace with _continuation → Spec Agent dispatched, session survives."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    RoleSession(wt, AgentRole.IMPROVE, "candidate/0").write_continuation(
        "session-token"
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)

    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert len(runner.calls) == 2


def test_spec_phase_restarts_from_phase_1_when_candidate_namespace_has_no_continuation(
    tmp_path, git_svc
):
    """AC2: candidate namespace without _continuation → restart notice, session discarded,
    next improve pass begins at Scan Agent."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    status_display = MagicMock()
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, status_display=status_display)

    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert runner.calls == []
    status_display.print.assert_any_call(
        "Improve",
        "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
    )
    assert not RoleSession(wt, AgentRole.IMPROVE).path.exists()

    follow_up = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    follow_up_deps = _make_deps(tmp_path, follow_up, git_svc=git_svc)
    _run(follow_up_deps)
    assert follow_up.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN


def test_spec_phase_dispatches_regardless_of_service_sidecar(tmp_path, git_svc):
    """AC3: gate checks continuation only; service sidecar is immaterial."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    # _continuation only — no codex thread_id sidecar or rollout files seeded
    RoleSession(wt, AgentRole.IMPROVE, "candidate/0").write_continuation(
        "session-token"
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    cfg = Config(improve_override=StageOverride(service="codex", effort="medium"))
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=cfg)

    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert len(runner.calls) == 2


def test_improve_resumes_at_report_after_scan_no_candidate(tmp_path, git_svc):
    """Resume from no-candidate candidate list (cursor=0) starts at phase 4 (report)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(
        RoleSession(wt, AgentRole.IMPROVE).path, [], no_candidate=True, cursor=0
    )
    runner = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_NO_CANDIDATE
    assert len(runner.calls) == 1


def test_no_candidate_list_starts_at_scan_regardless_of_other_files(tmp_path, git_svc):
    """Without a candidate list on disk, improve always starts from scan."""
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN
    assert len(runner.calls) == 3


def test_improve_resumes_at_issues_mid_phase(tmp_path, git_svc):
    """Candidate with a record (PRD done) and in-flight='03-issues' resumes at Issues phase."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_candidate_record(RoleSession(wt, AgentRole.IMPROVE).path, 0)
    ImproveRoleSessionStore(RoleSession(wt, AgentRole.IMPROVE).path).write_in_flight(
        "03-issues"
    )
    runner = _make_runner_with_drafts(CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_TICKETS
    assert runner.calls[0].prompt.scope_args["IMPROVE_SHORT_SID"] != ""
    assert len(runner.calls) == 1


def test_improve_resumes_mid_phase_2_without_clean_entry_gate(tmp_path, git_svc):
    """Candidate with no record and in-flight='02-spec': PRD resumes as a continuation (no role prompt)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    ImproveRoleSessionStore(RoleSession(wt, AgentRole.IMPROVE).path).write_in_flight(
        "02-spec"
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)

    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert runner.calls[0].prompt.kind is PromptKind.ROLE_PROMPT
    assert len(runner.calls) == 2


def test_improve_is_terminal_after_issues(tmp_path, git_svc):
    """All candidates filed (cursor past end) is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(
        RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE], cursor=1
    )
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert len(runner.calls) == 0


def test_improve_is_terminal_after_report(tmp_path, git_svc):
    """No-candidate with cursor past end is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(
        RoleSession(wt, AgentRole.IMPROVE).path, [], no_candidate=True, cursor=1
    )
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert len(runner.calls) == 0


# ── Issue #528: phase-boundary prompt shape ──────────────────────────────────


def test_mid_phase_2_retry_is_role_prompt_kind(tmp_path, git_svc):
    """Resume mid-phase-2 (in-flight='02-spec'): kind=ROLE_PROMPT so the runner
    falls back to the continuation prompt (role prompt already in history)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    ImproveRoleSessionStore(RoleSession(wt, AgentRole.IMPROVE).path).write_in_flight(
        "02-spec"
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    prd_call = next(
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_SPEC
    )
    assert prd_call.prompt.kind is PromptKind.ROLE_PROMPT


def test_cross_teardown_resume_at_phase_2_is_follow_up_kind(tmp_path, git_svc):
    """Resume with candidate list (scan done, no in-flight): PRD's kind=FOLLOW_UP
    so the PRD prompt is delivered, not the continuation prompt."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="opencode",
        provider_session_id="sess-opencode-123",
        namespace="candidate/0",
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=Config(improve_override=StageOverride(service="opencode", effort="medium")),
        service_registry=ServiceRegistry({"opencode": OpenCodeService()}),
    )
    _run(deps)
    prd_call = next(
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_SPEC
    )
    assert prd_call.prompt.kind is PromptKind.FOLLOW_UP


def test_cold_start_phase_1_is_role_prompt_kind(deps, agent_runner):
    """Cold start: phase 1 RunRequest has kind=ROLE_PROMPT so Fresh-run
    prompt-shape stays identical."""
    _run(deps)
    scan_call = agent_runner.calls[0]
    assert scan_call.prompt.kind is PromptKind.ROLE_PROMPT


def test_phase_2_is_follow_up_kind_on_resumed_session(deps, agent_runner):
    """After phase 1 completes cleanly, phase 2's RunRequest has kind=FOLLOW_UP
    so the PRD prompt is delivered rather than the continuation prompt."""
    _run(deps)
    prd_call = next(
        c
        for c in agent_runner.calls
        if c.prompt.template == PromptTemplate.IMPROVE_SPEC
    )
    assert prd_call.prompt.kind is PromptKind.FOLLOW_UP


def test_improve_fresh_run_on_malformed_progress(tmp_path, git_svc):
    """Malformed progress file falls back to a fresh run starting at phase 1 (scan)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = RoleSession(wt, AgentRole.IMPROVE).path
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (wt / "pyproject.toml").write_text("[project]\nname='t'\n")
    (role_session_dir / "_phase_progress").write_text(
        "corrupted-data", encoding="utf-8"
    )
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN


def test_improve_fresh_run_on_empty_progress_file(tmp_path, git_svc):
    """No candidate list on disk → fresh run starting at scan (even if other files present)."""
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN


def test_improve_fresh_run_on_whitespace_only_progress_file(tmp_path, git_svc):
    """Malformed candidate list JSON falls back to fresh scan."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = RoleSession(wt, AgentRole.IMPROVE).path
    _write_malformed_candidate_list(role_session_dir, "\n  \t  \n")
    (role_session_dir / "_fingerprint").write_text("abc123", encoding="utf-8")
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN


def test_improve_resumes_correctly_with_whitespace_padded_progress(tmp_path, git_svc):
    """Whitespace-padded cursor file value is parsed correctly — resumes at PRD phase."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    # Overwrite cursor with whitespace-padded integer
    _overwrite_candidate_cursor_raw(RoleSession(wt, AgentRole.IMPROVE).path, "  0  \n")
    _seed_exact_phase_1_main_transcript(
        wt, service_name="opencode", provider_session_id="sess-opencode-123"
    )
    # Simulate the post-fork state: candidate/0 namespace forked from main.
    RoleSession(wt, AgentRole.IMPROVE, "main").fork_namespace("candidate/0")
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=Config(improve_override=StageOverride(service="opencode", effort="medium")),
        service_registry=ServiceRegistry({"opencode": OpenCodeService()}),
    )
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert len(runner.calls) == 2


# ── Session namespace per phase ───────────────────────────────────────────────


def test_improve_phases_01_and_04_use_main_namespace(tmp_path, git_svc):
    """Phases 01-scan and 04-no-candidate-report use session_namespace='main'."""
    no_candidate_cfg = Config(logs_dir=tmp_path, diagnose_on_failure=True)
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()],  # 01-scan NO-CANDIDATE → 04-report
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=no_candidate_cfg)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN
    assert runner.calls[0].session_namespace == "main"
    assert runner.calls[1].prompt.template == PromptTemplate.IMPROVE_NO_CANDIDATE
    assert runner.calls[1].session_namespace == "main"


def test_improve_phase_02_uses_candidate_namespace(deps, agent_runner):
    """Phase 02-prd uses a per-candidate session namespace ('candidate/0' for first)."""
    _run(deps)
    prd_call = next(
        c
        for c in agent_runner.calls
        if c.prompt.template == PromptTemplate.IMPROVE_SPEC
    )
    assert prd_call.session_namespace == "candidate/0"


def test_improve_phase_03_uses_candidate_namespace(deps, agent_runner):
    """Phase 03-issues uses a per-candidate session namespace ('candidate/0' for first)."""
    _run(deps)
    issues_call = next(
        c
        for c in agent_runner.calls
        if c.prompt.template == PromptTemplate.IMPROVE_TICKETS
    )
    assert issues_call.session_namespace == "candidate/0"


def test_improve_all_phases_have_correct_namespace(deps, agent_runner):
    """Happy path: scan uses 'main'; prd/issues use per-candidate namespace."""
    _run(deps)
    assert agent_runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN
    assert agent_runner.calls[0].session_namespace == "main"
    assert agent_runner.calls[1].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert agent_runner.calls[1].session_namespace == "candidate/0"
    assert agent_runner.calls[2].prompt.template == PromptTemplate.IMPROVE_TICKETS
    assert agent_runner.calls[2].session_namespace == "candidate/0"


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


# ── Fingerprint gate: safe-SHA gating ────────────────────────────────────────


def test_fingerprint_gate_discards_session_when_safe_sha_changes(tmp_path, git_svc):
    """When safe SHA changes between runs, session is discarded; ImprovePhaseDriver starts fresh at scan."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = RoleSession(wt, AgentRole.IMPROVE).path
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (wt / "pyproject.toml").write_text("[project]\nname='t'\n")
    (role_session_dir / "_phase_progress").write_text(
        "01-scan:picked", encoding="utf-8"
    )
    (role_session_dir / "_fingerprint").write_text("old-sha-xyz", encoding="utf-8")

    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN
    assert len(runner.calls) == 3


def test_fingerprint_gate_preserves_session_when_safe_sha_unchanged(tmp_path, git_svc):
    """When safe SHA is unchanged, session state (candidate list) survives into the new run."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(
        RoleSession(wt, AgentRole.IMPROVE).path, [], no_candidate=True, cursor=0
    )

    runner = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_NO_CANDIDATE
    assert len(runner.calls) == 1


# ── Behavior 4: absent candidates block on scan raises AgentOutputProtocolError


def test_scan_without_candidates_block_raises_protocol_error(tmp_path, git_svc):
    """Scan completing with COMPLETE but no candidates block raises AgentOutputProtocolError.

    FakeAgentRunner returns CompletionOutput (simulating an agent that emitted
    COMPLETE without a <candidates> block). The driver must raise rather than
    silently proceeding to phase 2.
    """
    runner = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    with pytest.raises(AgentOutputProtocolError):
        _run(deps)


def test_scan_returning_candidates_output_continues_to_phase_2(tmp_path, git_svc):
    """Scan returning ScanCandidatesOutput proceeds normally to phase 2."""
    candidates = ScanCandidatesOutput(
        candidates=(ScanCandidateItem(rank=1, title="Refactor seam"),)
    )
    runner = _make_runner_with_drafts(
        candidates, CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN
    assert runner.calls[1].prompt.template == PromptTemplate.IMPROVE_SPEC


def _make_runner_with_drafts(
    *responses: object,
    preflight_responses: list[list[PreflightCommandFailure] | BaseException]
    | None = None,
) -> FakeAgentRunner:
    """FakeAgentRunner that writes valid spec draft when the issues phase is called."""
    if preflight_responses is None:
        preflight_responses = [[]]
    resp_list = list(responses)
    idx = [0]

    def _side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            _write_spec_draft(
                _draft_dir(RoleSession(request.mount_path, AgentRole.IMPROVE).path)
            )
        result = resp_list[idx[0]]
        idx[0] += 1
        return result

    return FakeAgentRunner(
        side_effect=_side_effect, preflight_responses=preflight_responses
    )


def test_draft_dir_is_at_role_level_not_namespace(tmp_path, git_svc):
    """Draft files live in the role session dir, not inside the 'main' namespace subdir."""
    call_count = [0]

    def side_effect(request):
        call_count[0] += 1
        if call_count[0] == 1:
            return make_scan_output()
        if call_count[0] == 3:
            draft_dir = _draft_dir(
                RoleSession(request.mount_path, AgentRole.IMPROVE).path
            )
            _write_spec_draft(draft_dir)
            _write_slice_draft(draft_dir, "01-first-slice")
        return CompletionOutput()

    github_svc = _make_filing_github_svc()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)
    _run(deps)

    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session = RoleSession(wt, AgentRole.IMPROVE).path
    main_namespace = role_session / "main"
    draft_dir = role_session / "_drafts"
    # Verify the draft dir is NOT inside the main namespace
    assert not draft_dir.is_relative_to(main_namespace)


# ── Multi-candidate: AC1, AC2, AC3, AC4, AC5, AC6, AC7 ───────────────────────


def test_multi_candidate_run_files_both_candidates_specs(tmp_path, git_svc):
    """Scan nominating 2 candidates: both candidates have their spec filed."""
    call_count = [0]

    def side_effect(request):
        call_count[0] += 1
        if request.prompt.template == PromptTemplate.IMPROVE_SCAN:
            return ScanCandidatesOutput(
                candidates=(
                    ScanCandidateItem(rank=1, title="First"),
                    ScanCandidateItem(rank=2, title="Second"),
                )
            )
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            draft_dir = _draft_dir(
                RoleSession(request.mount_path, AgentRole.IMPROVE).path
            )
            _write_spec_draft(draft_dir)
        return CompletionOutput()

    github_svc = _make_filing_github_svc()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    # 2 candidates x 1 spec each = 2 create_issue_in calls
    assert github_svc.create_issue_in.call_count == 2


def test_multi_candidate_run_files_all_candidates_in_rank_order(tmp_path, git_svc):
    """A scan nominating two candidates files both, candidate/0 before candidate/1."""
    scan_output = ScanCandidatesOutput(
        candidates=(
            ScanCandidateItem(rank=1, title="First"),
            ScanCandidateItem(rank=2, title="Second"),
        )
    )
    issues_namespaces: list[str] = []

    def side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_SCAN:
            return scan_output
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            issues_namespaces.append(request.session_namespace)
            _write_spec_draft(
                _draft_dir(RoleSession(request.mount_path, AgentRole.IMPROVE).path)
            )
        return CompletionOutput()

    github_svc = _make_filing_github_svc()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)
    _run(deps)

    assert issues_namespaces == ["candidate/0", "candidate/1"]
    assert github_svc.create_issue_in.call_count == 2


def test_dispatch_count_increments_per_completed_candidate(tmp_path, git_svc):
    """improve_dispatched_count rises by the number of candidates completed in one run."""
    scan_output = ScanCandidatesOutput(
        candidates=(
            ScanCandidateItem(rank=1, title="First"),
            ScanCandidateItem(rank=2, title="Second"),
        )
    )

    def side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_SCAN:
            return scan_output
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            _write_spec_draft(
                _draft_dir(RoleSession(request.mount_path, AgentRole.IMPROVE).path)
            )
        return CompletionOutput()

    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert result.completed_count == 2


def test_no_candidate_path_makes_no_forks(tmp_path, git_svc):
    """A scan that nominates nothing does not dispatch any per-candidate work."""
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    dispatched_templates = [c.prompt.template for c in runner.calls]
    assert PromptTemplate.IMPROVE_SPEC not in dispatched_templates
    assert PromptTemplate.IMPROVE_TICKETS not in dispatched_templates


def test_gate_failure_for_next_candidate_ends_dispatch_without_touching_filed(
    tmp_path, git_svc
):
    """AC4: gate fails for candidate/1 → dispatch ends; candidate/0 already filed is untouched."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    # Simulate: scan done with 2 candidates, candidate/0 fully filed (cursor=1)
    two_candidates = [
        ScanCandidateItem(rank=1, title="First"),
        ScanCandidateItem(rank=2, title="Second"),
    ]
    _seed_candidate_list(
        RoleSession(wt, AgentRole.IMPROVE).path, two_candidates, cursor=1
    )
    _seed_candidate_record(
        RoleSession(wt, AgentRole.IMPROVE).path, 0, spec_number=101, labels_applied=True
    )
    # candidate/1 transcript deliberately NOT seeded → gate will fail
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=Config(improve_override=StageOverride(service="opencode", effort="medium")),
        service_registry=ServiceRegistry({"opencode": OpenCodeService()}),
    )
    result = _run(deps)
    # Gate failed → no candidates dispatched this call
    assert isinstance(result, ImproveContinue)
    assert result.completed_count == 0
    # No PRD or Issues agents ran (candidate/0 was already filed, candidate/1 blocked)
    assert runner.calls == []


def test_cross_teardown_resume_checks_candidate_namespace_for_gate(tmp_path, git_svc):
    """Resume with candidate list (scan done) checks 'candidate/0' transcript, not 'main'."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    # Seed 'candidate/0' namespace transcript (fork of main)
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="opencode",
        provider_session_id="sess-123",
        namespace="candidate/0",
    )
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        cfg=Config(improve_override=StageOverride(service="opencode", effort="medium")),
        service_registry=ServiceRegistry({"opencode": OpenCodeService()}),
    )
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SPEC
    assert len(runner.calls) == 2


# ── Safe-SHA wind-down (issue #2098) ─────────────────────────────────────────


def _make_two_candidate_runner() -> FakeAgentRunner:
    scan_output = ScanCandidatesOutput(
        candidates=(
            ScanCandidateItem(rank=1, title="First"),
            ScanCandidateItem(rank=2, title="Second"),
        )
    )

    def side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_SCAN:
            return scan_output
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            _write_spec_draft(
                _draft_dir(RoleSession(request.mount_path, AgentRole.IMPROVE).path)
            )
        return CompletionOutput()

    return FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])


def test_sha_unchanged_run_is_unaffected(tmp_path, git_svc):
    """AC5: A run whose safe SHA does not change processes all candidates normally."""
    runner = _make_two_candidate_runner()
    github_svc = _make_filing_github_svc()
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert result.completed_count == 2
    issues_calls = [
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_TICKETS
    ]
    assert len(issues_calls) == 2


# ── Issue #2191: Improve phase row — constant name, live body ─────────────────


def _three_candidate_runner_with_drafts(
    *, preflight_responses: list | None = None
) -> FakeAgentRunner:
    """FakeAgentRunner for a 3-candidate scan; writes drafts on each issues call."""
    scan_output = ScanCandidatesOutput(
        candidates=(
            ScanCandidateItem(rank=1, title="First candidate"),
            ScanCandidateItem(rank=2, title="Second candidate"),
            ScanCandidateItem(rank=3, title="Third candidate"),
        )
    )

    def side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_SCAN:
            return scan_output
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            _write_spec_draft(
                _draft_dir(RoleSession(request.mount_path, AgentRole.IMPROVE).path)
            )
        return CompletionOutput()

    return FakeAgentRunner(
        side_effect=side_effect,
        preflight_responses=preflight_responses
        if preflight_responses is not None
        else [[]],
    )


def test_improve_row_name_is_improve_no_counter_with_improve_max(tmp_path, git_svc):
    """With improve_max set, the Improve row registers under 'Improve'; no row name has a counter."""
    status_display = RecordingStatusDisplay()
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    cfg = Config(improve_max=10)
    deps = _make_deps(
        tmp_path, runner, git_svc=git_svc, status_display=status_display, cfg=cfg
    )
    _run(deps)

    caller_names = [c["caller"] for c in status_display.register_calls]
    assert "Improve" in caller_names
    assert not any("(" in name for name in caller_names)


def test_improve_phase_body_scanning_before_scan_agent_runs(tmp_path, git_svc):
    """The Improve phase body reads 'scanning for candidates' from the start (before scan returns)."""
    status_display = RecordingStatusDisplay()
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, status_display=status_display)
    _run(deps)

    improve_updates = [
        body for name, body in status_display.phase_updates if name == "Improve"
    ]
    assert improve_updates[0] == "scanning for candidates"


def test_improve_phase_body_first_candidate_with_improve_max(tmp_path, git_svc):
    """First candidate of 3 with improve_max=10 and d=0: body reads 'candidate 1/3 · improvement 1/10'."""
    status_display = RecordingStatusDisplay()
    runner = _three_candidate_runner_with_drafts()
    cfg = Config(improve_max=10)
    github_svc = _make_filing_github_svc()
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        status_display=status_display,
        cfg=cfg,
        github_svc=github_svc,
    )
    _run(deps)

    improve_updates = [
        body for name, body in status_display.phase_updates if name == "Improve"
    ]
    assert "candidate 1/3 · improvement 1/10" in improve_updates


def test_improve_phase_body_changes_for_second_candidate(tmp_path, git_svc):
    """Body changes within a single improve_phase call: second candidate shows 'candidate 2/3 · improvement 2/10'."""
    status_display = RecordingStatusDisplay()
    runner = _three_candidate_runner_with_drafts()
    cfg = Config(improve_max=10)
    github_svc = _make_filing_github_svc()
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        status_display=status_display,
        cfg=cfg,
        github_svc=github_svc,
    )
    _run(deps)

    improve_updates = [
        body for name, body in status_display.phase_updates if name == "Improve"
    ]
    idx1 = improve_updates.index("candidate 1/3 · improvement 1/10")
    idx2 = improve_updates.index("candidate 2/3 · improvement 2/10")
    assert idx1 < idx2


def test_improve_phase_body_candidate_without_improve_max(tmp_path, git_svc):
    """With improve_max unset, body while working second of three candidates reads 'candidate 2/3'."""
    status_display = RecordingStatusDisplay()
    runner = _three_candidate_runner_with_drafts()
    github_svc = _make_filing_github_svc()
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        status_display=status_display,
        github_svc=github_svc,
    )
    _run(deps)

    improve_updates = [
        body for name, body in status_display.phase_updates if name == "Improve"
    ]
    assert "candidate 2/3" in improve_updates
    second_body = next(b for b in improve_updates if b.startswith("candidate 2/3"))
    assert "improvement" not in second_body


def test_improve_phase_prints_candidate_start_line(tmp_path, git_svc):
    """Each candidate start emits a print on 'Improve' naming ordinal and title."""
    status_display = RecordingStatusDisplay()
    runner = _three_candidate_runner_with_drafts()
    github_svc = _make_filing_github_svc()
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        status_display=status_display,
        github_svc=github_svc,
    )
    _run(deps)

    printed = [
        msg
        for event, caller, msg, *_ in status_display.calls
        if event == "print" and caller == "Improve"
    ]
    assert any('→ candidate 2/3 "Second candidate"' in str(m) for m in printed)


def test_improve_phase_closes_with_filed_count(tmp_path, git_svc):
    """A run that files two improvements closes the Improve row with 'filed 2 improvement(s)'."""
    scan_output = ScanCandidatesOutput(
        candidates=(
            ScanCandidateItem(rank=1, title="First"),
            ScanCandidateItem(rank=2, title="Second"),
        )
    )

    def side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_SCAN:
            return scan_output
        if request.prompt.template == PromptTemplate.IMPROVE_TICKETS:
            _write_spec_draft(
                _draft_dir(RoleSession(request.mount_path, AgentRole.IMPROVE).path)
            )
        return CompletionOutput()

    status_display = RecordingStatusDisplay()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    github_svc = _make_filing_github_svc()
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        status_display=status_display,
        github_svc=github_svc,
    )
    _run(deps)

    improve_close = next(
        c for c in status_display.remove_calls if c["caller"] == "Improve"
    )
    assert improve_close["shutdown_message"] == "filed 2 improvement(s)"


def test_improve_phase_no_candidate_closes_with_no_candidate(tmp_path, git_svc):
    """Scan that nominates nothing closes the Improve row with 'no candidate'."""
    status_display = RecordingStatusDisplay()
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, status_display=status_display)
    _run(deps)

    improve_close = next(
        c for c in status_display.remove_calls if c["caller"] == "Improve"
    )
    assert improve_close["shutdown_message"] == "no candidate"


def test_improve_phase_no_candidate_body_filing_report(tmp_path, git_svc):
    """While the no-candidate report agent runs, the Improve body reads 'filing no-candidate report'."""
    status_display = RecordingStatusDisplay()
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, status_display=status_display)
    _run(deps)

    improve_updates = [
        body for name, body in status_display.phase_updates if name == "Improve"
    ]
    assert "filing no-candidate report" in improve_updates


# ── Issue #2244: Resume announcement ─────────────────────────────────────────


def test_mid_phase_2_resume_prints_resume_announcement(tmp_path, git_svc):
    """Resuming mid-phase-2 prints a status line naming candidate ordinal, title, and phase."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    ImproveRoleSessionStore(RoleSession(wt, AgentRole.IMPROVE).path).write_in_flight(
        "02-spec"
    )
    status_display = RecordingStatusDisplay()
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, status_display=status_display)

    _run(deps)

    printed = [
        msg
        for event, caller, msg, *_ in status_display.calls
        if event == "print" and caller == "Improve"
    ]
    assert any(
        "resuming" in str(m) and "1/1" in str(m) and "Seeded candidate" in str(m)
        for m in printed
    )


def test_mid_phase_3_resume_prints_resume_announcement(tmp_path, git_svc):
    """Resuming mid-phase-3 prints a status line naming candidate ordinal, title, and phase."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(RoleSession(wt, AgentRole.IMPROVE).path, [_DEFAULT_CANDIDATE])
    _seed_candidate_record(RoleSession(wt, AgentRole.IMPROVE).path, 0)
    ImproveRoleSessionStore(RoleSession(wt, AgentRole.IMPROVE).path).write_in_flight(
        "03-tickets"
    )
    status_display = RecordingStatusDisplay()
    runner = _make_runner_with_drafts(CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, status_display=status_display)

    _run(deps)

    printed = [
        msg
        for event, caller, msg, *_ in status_display.calls
        if event == "print" and caller == "Improve"
    ]
    assert any(
        "resuming" in str(m) and "1/1" in str(m) and "Seeded candidate" in str(m)
        for m in printed
    )


def test_fresh_improve_cycle_prints_no_resume_announcement(tmp_path, git_svc):
    """A fresh improve cycle (no mid-candidate in-flight state) prints no resume announcement."""
    status_display = RecordingStatusDisplay()
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, status_display=status_display)

    _run(deps)

    printed = [
        msg
        for event, caller, msg, *_ in status_display.calls
        if event == "print" and caller == "Improve"
    ]
    assert not any("resuming" in str(m) for m in printed)
