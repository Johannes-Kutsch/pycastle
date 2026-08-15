"""Tests for improve_phase: multi-prompt Work-phase, phase progress file, NO-CANDIDATE protocol."""

import asyncio
import dataclasses
import json
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
from pycastle.iteration.improve_drafts import DraftSetValidationError
from pycastle.iteration.improve_filing import _CandidateRecord, _save_record
from pycastle.iteration.preflight import PreflightReady
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import GithubNetworkError, ServiceRegistry
from pycastle.services.runtime_services import CodexService, OpenCodeService
from pycastle.session import RoleSession
from pycastle.session.role import session_uuid_for_role_session_path
from pycastle.session.service_session_store import (
    save_service_session_id,
    save_service_session_metadata,
)
from tests.support import (
    FakeAgentRunner,
    _make_deps,
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
        if request.prompt.template == PromptTemplate.IMPROVE_ISSUES:
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            draft_dir.mkdir(parents=True, exist_ok=True)
            body = "A" * 120
            (draft_dir / "spec.md").write_text(
                f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\n{body}"
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


def _role_session_session_uuid(role_session: object) -> str:
    role_session_path = getattr(role_session, "path", None)
    if isinstance(role_session_path, Path):
        identity_uuid = session_uuid_for_role_session_path(role_session_path)
        if identity_uuid is not None:
            return identity_uuid
    legacy = getattr(role_session, "session_uuid", None)
    if callable(legacy):
        return legacy()
    raise AssertionError("Unable to derive role session identifier")


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
    """Picked path runs IMPROVE_SCAN then IMPROVE_PRD in order."""
    _run(deps)
    templates = [c.prompt.template for c in agent_runner.calls]
    assert templates[:2] == [PromptTemplate.IMPROVE_SCAN, PromptTemplate.IMPROVE_PRD]


@pytest.mark.parametrize(
    ("template", "expected_name", "expected_body"),
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
    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    assert not role_session_dir.exists()


def test_improve_phase_candidate_list_written_with_no_candidate_flag_after_scan(
    tmp_path, git_svc
):
    """After NO-CANDIDATE scan, candidate list is written with no_candidate=True."""
    candidate_list_values: list[bool] = []
    worktree_path = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    list_file = worktree_path / ".pycastle-session" / "improve" / "_candidate_list"

    call_count = 0

    def _side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return NoCandidateOutput()
        # Read candidate list before second call executes
        if list_file.exists():
            data = json.loads(list_file.read_text(encoding="utf-8"))
            candidate_list_values.append(data.get("no_candidate", False))
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
        (AgentRole.IMPROVE, PromptTemplate.IMPROVE_PRD),
        (AgentRole.IMPROVE, PromptTemplate.IMPROVE_ISSUES),
    ]


def test_improve_phase_dispatches_prd_step_with_expected_work_body(tmp_path, git_svc):
    github_svc = MagicMock()
    github_svc.get_recent_improve_prds.return_value = [
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
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.work_body == "writing PRD"


def test_improve_phase_still_dispatches_prd_step_when_recent_prd_history_is_empty(
    tmp_path, git_svc
):
    github_svc = MagicMock()
    github_svc.get_recent_improve_prds.return_value = []
    github_svc.create_issue_in.return_value = (0, 0)
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    prd_call = next(
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.prompt.template == PromptTemplate.IMPROVE_PRD


def test_improve_phase_propagates_recent_improve_prd_lookup_failures(tmp_path, git_svc):
    github_svc = MagicMock()
    github_svc.get_recent_improve_prds.side_effect = GithubNetworkError(
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
    github_svc.get_recent_improve_prds.side_effect = [
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
    github_svc.get_recent_improve_prds.side_effect = [
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


def _seed_candidate_list(
    worktree_path: Path,
    candidates: list[ScanCandidateItem],
    *,
    no_candidate: bool = False,
    cursor: int = 0,
    fingerprint: str | None = "abc123",
) -> None:
    """Pre-seed the candidate list and cursor to simulate a prior scan.

    Also writes a fingerprint so the fingerprint gate does not discard the
    session on entry.
    """
    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "candidates": [{"rank": c.rank, "title": c.title} for c in candidates]
    }
    if no_candidate:
        data["no_candidate"] = True
    (role_session_dir / "_candidate_list").write_text(
        json.dumps(data), encoding="utf-8"
    )
    (role_session_dir / "_candidate_cursor").write_text(str(cursor), encoding="utf-8")
    if fingerprint is not None:
        (role_session_dir / "_fingerprint").write_text(fingerprint, encoding="utf-8")


def _seed_candidate_record(
    worktree_path: Path,
    idx: int,
    *,
    prd_number: int | None = None,
    spec_number: int | None = None,
    labels_applied: bool = False,
) -> None:
    """Pre-seed a per-candidate record (simulates the filing pass having run)."""
    role_session_dir = worktree_path / ".pycastle-session" / "improve"
    candidate_dir = role_session_dir / "candidates" / str(idx)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    record = _CandidateRecord(
        prd_number=prd_number,
        spec_number=spec_number,
        spec_database_id=42 if spec_number is not None else None,
        spec_title="Seeded" if spec_number is not None else "",
        filed_slices=[],
        labels_applied=labels_applied,
    )
    _save_record(candidate_dir, record)


_DEFAULT_CANDIDATE = ScanCandidateItem(rank=1, title="Seeded candidate")


def _seed_exact_phase_1_main_transcript(
    worktree_path: Path,
    *,
    service_name: str,
    provider_session_id: str,
    namespace: str = "main",
) -> None:
    role_session = RoleSession(worktree_path, AgentRole.IMPROVE, namespace)
    save_service_session_metadata(role_session.path, service_name, provider_session_id)
    if service_name == "opencode":
        state_dir = worktree_path / "opencode"
    elif service_name == "codex":
        state_dir = role_session.path / service_name
    else:
        state_dir = role_session.path / service_name
    state_dir.mkdir(parents=True, exist_ok=True)
    save_service_session_id(role_session.path, service_name, provider_session_id)
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


def test_improve_resumes_at_prd_after_scan_picked(tmp_path, git_svc):
    """Resume with candidate list (scan done, cursor=0, no record) starts at PRD."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
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
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_PRD
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_dispatches_prd_prompt_for_exact_codex_transcript(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
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

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_PRD
    assert runner.calls[0].prompt.send_role_prompt_on_resume is True
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_accepts_recovered_exact_codex_transcript(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="codex",
        provider_session_id="thread-exact",
        namespace="candidate/0",
    )
    (
        wt / ".pycastle-session" / "improve" / "candidate" / "0" / "codex" / "thread_id"
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

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_PRD
    assert runner.calls[0].prompt.send_role_prompt_on_resume is True
    assert len(runner.calls) == 2


def test_improve_clean_phase_2_entry_restarts_when_codex_rollout_thread_is_not_exact(
    tmp_path, git_svc
):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="codex",
        provider_session_id="thread-recorded",
    )
    rollout_path = (
        wt
        / ".pycastle-session"
        / "improve"
        / "main"
        / "codex"
        / "sessions"
        / "2026"
        / "05"
        / "30"
        / "rollout-001.jsonl"
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
    assert not (wt / ".pycastle-session" / "improve").exists()


def test_improve_gate_failure_restarts_next_entry_from_scan_phase(tmp_path, git_svc):
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
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
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
    _seed_exact_phase_1_main_transcript(
        wt,
        service_name="claude",
        provider_session_id=_role_session_session_uuid(
            RoleSession(wt, AgentRole.IMPROVE, "main")
        ),
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
    assert not (wt / ".pycastle-session" / "improve").exists()


def test_improve_resumes_at_report_after_scan_no_candidate(tmp_path, git_svc):
    """Resume from no-candidate candidate list (cursor=0) starts at phase 4 (report)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [], no_candidate=True, cursor=0)
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
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
    _seed_candidate_record(wt, 0, prd_number=None)
    role_session_dir = wt / ".pycastle-session" / "improve"
    (role_session_dir / "_phase_in_flight").write_text("03-issues", encoding="utf-8")
    runner = _make_runner_with_drafts(CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_ISSUES
    assert runner.calls[0].prompt.scope_args["IMPROVE_SHORT_SID"] != ""
    assert len(runner.calls) == 1


def test_improve_resumes_mid_phase_2_without_clean_entry_gate(tmp_path, git_svc):
    """Candidate with no record and in-flight='02-prd': PRD resumes as a continuation (no role prompt)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
    role_session_dir = wt / ".pycastle-session" / "improve"
    (role_session_dir / "_phase_in_flight").write_text("02-prd", encoding="utf-8")
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)

    _run(deps)

    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_PRD
    assert runner.calls[0].prompt.send_role_prompt_on_resume is False
    assert len(runner.calls) == 2


def test_improve_is_terminal_after_issues(tmp_path, git_svc):
    """All candidates filed (cursor past end) is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE], cursor=1)
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert len(runner.calls) == 0


def test_improve_is_terminal_after_report(tmp_path, git_svc):
    """No-candidate with cursor past end is immediately terminal — no agent calls."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [], no_candidate=True, cursor=1)
    runner = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    assert len(runner.calls) == 0


# ── Issue #528: phase-boundary prompt shape ──────────────────────────────────


def test_mid_phase_2_retry_does_not_signal_role_prompt(tmp_path, git_svc):
    """Resume mid-phase-2 (in-flight='02-prd'): send_role_prompt_on_resume stays False
    so the runner falls back to the continuation prompt (role prompt already in history)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
    role_session_dir = wt / ".pycastle-session" / "improve"
    (role_session_dir / "_phase_in_flight").write_text("02-prd", encoding="utf-8")
    runner = _make_runner_with_drafts(CompletionOutput(), CompletionOutput())
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    prd_call = next(
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.prompt.send_role_prompt_on_resume is False


def test_cross_teardown_resume_at_phase_2_signals_role_prompt(tmp_path, git_svc):
    """Resume with candidate list (scan done, no in-flight): PRD's send_role_prompt_on_resume=True
    so the PRD prompt is delivered, not the continuation prompt."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
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
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.prompt.send_role_prompt_on_resume is True


def test_cold_start_phase_1_does_not_signal_role_prompt_on_resume(deps, agent_runner):
    """Cold start: phase 1 RunRequest leaves send_role_prompt_on_resume False
    so today's Fresh-run prompt-shape stays identical."""
    _run(deps)
    scan_call = agent_runner.calls[0]
    assert scan_call.prompt.send_role_prompt_on_resume is False


def test_phase_2_signals_role_prompt_on_resumed_session(deps, agent_runner):
    """After phase 1 completes cleanly, phase 2's RunRequest signals that the
    new role prompt must be sent despite the resumed claude session — otherwise
    the agent would receive only the continuation prompt (issue #528)."""
    _run(deps)
    prd_call = next(
        c for c in agent_runner.calls if c.prompt.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.prompt.send_role_prompt_on_resume is True


def test_improve_fresh_run_on_malformed_progress(tmp_path, git_svc):
    """Malformed progress file falls back to a fresh run starting at phase 1 (scan)."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session_dir = wt / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
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
    role_session_dir = wt / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
    (role_session_dir / "_candidate_list").write_text("\n  \t  \n", encoding="utf-8")
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
    role_session_dir = wt / ".pycastle-session" / "improve"
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
    # Overwrite cursor with whitespace-padded integer
    (role_session_dir / "_candidate_cursor").write_text("  0  \n", encoding="utf-8")
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
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_PRD
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
        c for c in agent_runner.calls if c.prompt.template == PromptTemplate.IMPROVE_PRD
    )
    assert prd_call.session_namespace == "candidate/0"


def test_improve_phase_03_uses_candidate_namespace(deps, agent_runner):
    """Phase 03-issues uses a per-candidate session namespace ('candidate/0' for first)."""
    _run(deps)
    issues_call = next(
        c
        for c in agent_runner.calls
        if c.prompt.template == PromptTemplate.IMPROVE_ISSUES
    )
    assert issues_call.session_namespace == "candidate/0"


def test_improve_all_phases_have_correct_namespace(deps, agent_runner):
    """Happy path: scan uses 'main'; prd/issues use per-candidate namespace."""
    _run(deps)
    assert agent_runner.calls[0].prompt.template == PromptTemplate.IMPROVE_SCAN
    assert agent_runner.calls[0].session_namespace == "main"
    assert agent_runner.calls[1].prompt.template == PromptTemplate.IMPROVE_PRD
    assert agent_runner.calls[1].session_namespace == "candidate/0"
    assert agent_runner.calls[2].prompt.template == PromptTemplate.IMPROVE_ISSUES
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
    role_session_dir = wt / ".pycastle-session" / "improve"
    role_session_dir.mkdir(parents=True, exist_ok=True)
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
    _seed_candidate_list(wt, [], no_candidate=True, cursor=0)

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
    assert runner.calls[1].prompt.template == PromptTemplate.IMPROVE_PRD


# ── Host-side filing pass (AC3, AC5, AC6, AC7) ──────────────────────────────

_VALID_BODY = "A" * 120


def _write_spec_draft(draft_dir: Path, *, body: str = _VALID_BODY) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "spec.md").write_text(
        f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\n{body}"
    )


def _write_slice_draft(draft_dir: Path, name: str, *, body: str = _VALID_BODY) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / f"{name}.md").write_text(
        f"---\ntitle: {name} Slice\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\n{body}"
    )


def _draft_dir(tmp_path: Path) -> Path:
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    return wt / ".pycastle-session" / "improve" / "_drafts"


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
        if request.prompt.template == PromptTemplate.IMPROVE_ISSUES:
            _write_spec_draft(
                request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            )
        result = resp_list[idx[0]]
        idx[0] += 1
        return result

    return FakeAgentRunner(
        side_effect=_side_effect, preflight_responses=preflight_responses
    )


def _make_filing_github_svc() -> MagicMock:
    github_svc = MagicMock()
    github_svc.repo = "test/repo"
    github_svc.create_issue_in.side_effect = [
        (100, 1000),
        (101, 1001),
        (102, 1002),
    ]
    return github_svc


def test_host_files_issues_after_slice_phase_with_valid_drafts(tmp_path, git_svc):
    """After phase 03 completes, host reads draft files and files the spec and slices."""
    call_count = [0]

    def side_effect(request):
        call_count[0] += 1
        if call_count[0] == 1:
            return make_scan_output()
        if call_count[0] == 3:
            # Issues phase: agent writes valid drafts to sandbox
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            _write_spec_draft(draft_dir)
            _write_slice_draft(draft_dir, "01-first-slice")
        return CompletionOutput()

    github_svc = _make_filing_github_svc()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    assert github_svc.create_issue_in.call_count == 2


def test_host_does_not_file_when_no_drafts_present(tmp_path, git_svc):
    """When draft dir is absent, improve_phase raises rather than silently succeeding.

    Flow: scan → prd → issues (no drafts written) → correction reprompt (still no
    drafts written) → second read_draft_set raises DraftSetValidationError.
    """
    github_svc = _make_filing_github_svc()
    # 4 responses: scan, prd, issues, correction reprompt (none write drafts)
    runner = FakeAgentRunner(
        [
            make_scan_output(),
            CompletionOutput(),
            CompletionOutput(),
            CompletionOutput(),
        ],
        preflight_responses=[[]],
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    with pytest.raises(DraftSetValidationError):
        _run(deps)

    assert github_svc.create_issue_in.call_count == 0


def test_malformed_drafts_reprompt_agent_before_filing(tmp_path, git_svc):
    """When drafts fail validation, the agent is reprompted once; nothing is filed yet."""
    call_count = [0]

    def side_effect(request):
        call_count[0] += 1
        if call_count[0] == 1:
            return make_scan_output()
        if call_count[0] == 3:
            # Issues phase: agent writes invalid drafts (slice body too short)
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            _write_spec_draft(draft_dir)
            _write_slice_draft(draft_dir, "01-foo", body="Too short.")
        return CompletionOutput()

    github_svc = _make_filing_github_svc()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    with pytest.raises(DraftSetValidationError):
        _run(deps)

    # Agent is called 4 times total: scan, prd, issues, correction reprompt
    assert len(runner.calls) == 4
    assert github_svc.create_issue_in.call_count == 0


def test_valid_reprompt_gets_filed(tmp_path, git_svc):
    """After a correction reprompt produces valid drafts, the issues are filed."""
    call_count = [0]

    def side_effect(request):
        call_count[0] += 1
        draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
        if call_count[0] == 1:
            return make_scan_output()
        if call_count[0] == 3:
            # Issues phase: agent writes invalid drafts initially (slice body too short)
            _write_spec_draft(draft_dir)
            _write_slice_draft(draft_dir, "01-slice", body="Too short.")
        if call_count[0] == 4:
            # Correction call: fix the drafts
            _write_spec_draft(draft_dir)
            _write_slice_draft(draft_dir, "01-slice")
        return CompletionOutput()

    github_svc = _make_filing_github_svc()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    assert call_count[0] == 4
    assert github_svc.create_issue_in.call_count == 2


def test_draft_dir_is_at_role_level_not_namespace(tmp_path, git_svc):
    """Draft files live in the role session dir, not inside the 'main' namespace subdir."""
    call_count = [0]

    def side_effect(request):
        call_count[0] += 1
        if call_count[0] == 1:
            return make_scan_output()
        if call_count[0] == 3:
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            _write_spec_draft(draft_dir)
            _write_slice_draft(draft_dir, "01-first-slice")
        return CompletionOutput()

    github_svc = _make_filing_github_svc()
    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)
    _run(deps)

    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    role_session = wt / ".pycastle-session" / "improve"
    main_namespace = role_session / "main"
    draft_dir = _draft_dir(tmp_path)
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
        if request.prompt.template == PromptTemplate.IMPROVE_ISSUES:
            draft_dir = request.mount_path / ".pycastle-session" / "improve" / "_drafts"
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
        if request.prompt.template == PromptTemplate.IMPROVE_ISSUES:
            issues_namespaces.append(request.session_namespace)
            _write_spec_draft(
                request.mount_path / ".pycastle-session" / "improve" / "_drafts"
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
        if request.prompt.template == PromptTemplate.IMPROVE_ISSUES:
            _write_spec_draft(
                request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            )
        return CompletionOutput()

    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert result.completed_count == 2


def test_improve_stops_at_cap_after_completing_first_candidate(tmp_path, git_svc):
    """With improve_max=1 and 2 candidates nominated, only the first is dispatched."""
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
        if request.prompt.template == PromptTemplate.IMPROVE_ISSUES:
            issues_namespaces.append(request.session_namespace)
            _write_spec_draft(
                request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            )
        return CompletionOutput()

    runner = FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])
    cfg = Config(improve_max=1)
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, cfg=cfg)
    result = _run(deps)

    assert issues_namespaces == ["candidate/0"]
    assert isinstance(result, ImproveContinue)
    assert result.completed_count == 1


def test_no_candidate_path_makes_no_forks(tmp_path, git_svc):
    """A scan that nominates nothing does not dispatch any per-candidate work."""
    runner = FakeAgentRunner(
        [NoCandidateOutput(), CompletionOutput()], preflight_responses=[[]]
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc)
    _run(deps)
    dispatched_templates = [c.prompt.template for c in runner.calls]
    assert PromptTemplate.IMPROVE_PRD not in dispatched_templates
    assert PromptTemplate.IMPROVE_ISSUES not in dispatched_templates


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
    _seed_candidate_list(wt, two_candidates, cursor=1)
    _seed_candidate_record(wt, 0, spec_number=101, labels_applied=True)
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
    _seed_candidate_list(wt, [_DEFAULT_CANDIDATE])
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
    assert runner.calls[0].prompt.template == PromptTemplate.IMPROVE_PRD
    assert len(runner.calls) == 2


# ── Safe-SHA wind-down (issue #2098) ─────────────────────────────────────────


class _ChangingPreflightCache:
    """Preflight cache that returns a different SHA on the second call."""

    def __init__(
        self, initial_sha: str = "abc123", changed_sha: str = "new-sha"
    ) -> None:
        self._calls = 0
        self._initial_sha = initial_sha
        self._changed_sha = changed_sha

    async def get_safe_sha(self, deps):
        self._calls += 1
        sha = self._initial_sha if self._calls == 1 else self._changed_sha
        return PreflightReady(sha=sha)


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
        if request.prompt.template == PromptTemplate.IMPROVE_ISSUES:
            _write_spec_draft(
                request.mount_path / ".pycastle-session" / "improve" / "_drafts"
            )
        return CompletionOutput()

    return FakeAgentRunner(side_effect=side_effect, preflight_responses=[[]])


def test_sha_change_mid_run_stops_further_agent_dispatch(tmp_path, git_svc):
    """AC1: SHA change after completing a candidate stops dispatch for subsequent candidates."""
    runner = _make_two_candidate_runner()
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        preflight_cache=_ChangingPreflightCache(),
    )

    result = _run(deps)

    issues_calls = [
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_ISSUES
    ]
    assert len(issues_calls) == 1
    assert isinstance(result, ImproveContinue)
    assert result.completed_count == 1


def test_sha_change_mid_run_does_not_start_next_candidate(tmp_path, git_svc):
    """AC4: No PRD or Issues agent dispatched for candidates after the one in progress."""
    runner = _make_two_candidate_runner()
    deps = _make_deps(
        tmp_path,
        runner,
        git_svc=git_svc,
        preflight_cache=_ChangingPreflightCache(),
    )

    _run(deps)

    prd_calls = [
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_PRD
    ]
    assert len(prd_calls) == 1
    assert prd_calls[0].session_namespace == "candidate/0"


def test_sha_unchanged_run_is_unaffected(tmp_path, git_svc):
    """AC5: A run whose safe SHA does not change processes all candidates normally."""
    runner = _make_two_candidate_runner()
    github_svc = _make_filing_github_svc()
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    result = _run(deps)

    assert isinstance(result, ImproveContinue)
    assert result.completed_count == 2
    issues_calls = [
        c for c in runner.calls if c.prompt.template == PromptTemplate.IMPROVE_ISSUES
    ]
    assert len(issues_calls) == 2


def test_sha_change_fingerprint_gate_closes_spec_only_candidate(tmp_path, git_svc):
    """AC2: When SHA changes between runs and a candidate has spec but no slices, close the spec."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    two_candidates = [
        ScanCandidateItem(rank=1, title="First"),
        ScanCandidateItem(rank=2, title="Second"),
    ]
    # candidate 0 fully done (cursor=1); candidate 1 has spec but no slices
    _seed_candidate_list(wt, two_candidates, cursor=1, fingerprint="old-sha")
    _seed_candidate_record(wt, 0, spec_number=100, labels_applied=True)
    _seed_candidate_record(wt, 1, spec_number=200)  # spec only, no slices

    github_svc = _make_filing_github_svc()
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    # Default StubPreflightCache returns sha="abc123", different from "old-sha"
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    github_svc.close_issue.assert_called_once_with(200)


def test_sha_change_fingerprint_gate_no_close_when_no_spec(tmp_path, git_svc):
    """AC2 boundary: when next candidate has no spec filed, nothing is closed."""
    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    two_candidates = [
        ScanCandidateItem(rank=1, title="First"),
        ScanCandidateItem(rank=2, title="Second"),
    ]
    # candidate 0 fully done; candidate 1 not started (no record)
    _seed_candidate_list(wt, two_candidates, cursor=1, fingerprint="old-sha")
    _seed_candidate_record(wt, 0, spec_number=100, labels_applied=True)

    github_svc = _make_filing_github_svc()
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    github_svc.close_issue.assert_not_called()


def test_sha_change_fingerprint_gate_completes_partial_slices_by_host(
    tmp_path, git_svc
):
    """AC3: When some slices are filed but not labeled, host completes filing without agent."""
    from pycastle.iteration.improve_filing import _FiledIssue, _save_record

    wt = tmp_path / "pycastle" / ".worktrees" / "improve-sandbox"
    two_candidates = [
        ScanCandidateItem(rank=1, title="First"),
        ScanCandidateItem(rank=2, title="Second"),
    ]
    # candidate 0 done; candidate 1 has spec + 1 filed slice but not labeled
    _seed_candidate_list(wt, two_candidates, cursor=1, fingerprint="old-sha")
    _seed_candidate_record(wt, 0, spec_number=100, labels_applied=True)

    role_session_dir = wt / ".pycastle-session" / "improve"
    candidate_dir = role_session_dir / "candidates" / "1"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    from pycastle.iteration.improve_filing import _CandidateRecord as CR

    record = CR(
        spec_number=200,
        spec_database_id=2000,
        spec_title="Spec",
        filed_slices=[
            _FiledIssue(handle="slice-a", number=201, database_id=2001, title="Slice A")
        ],
        labels_applied=False,
    )
    _save_record(candidate_dir, record)

    # Write draft files with spec + 2 slices (slice-a already filed, slice-b not yet)
    draft_dir = role_session_dir / "_drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "slice-a")
    _write_slice_draft(draft_dir, "slice-b")

    github_svc = _make_filing_github_svc()
    runner = _make_runner_with_drafts(
        make_scan_output(), CompletionOutput(), CompletionOutput()
    )
    deps = _make_deps(tmp_path, runner, git_svc=git_svc, github_svc=github_svc)

    _run(deps)

    # Host completed the partial candidate: spec and existing slice must be labelled
    # without any agent dispatched against the old "candidate/1" session namespace.
    label_calls = github_svc.add_label_to_issue.call_args_list
    labeled_numbers = {call.args[0] for call in label_calls}
    assert 200 in labeled_numbers  # spec labelled by host
    assert 201 in labeled_numbers  # slice-a labelled by host
    github_svc.close_issue.assert_not_called()  # completed normally, not closed
    # No agent was dispatched to handle the partial candidate — wind-down is host-only
    old_ns_calls = [c for c in runner.calls if c.session_namespace == "candidate/1"]
    assert old_ns_calls == []
