"""Tests for improve_candidate_lifecycle module interface.

Covers: reconcile_and_wind_down (fingerprint gate, AC2/AC3 wind-down) and
file_and_decide (fresh filing, correction cap, cap-reached, safe-sha-changed,
advance, prev_spec chaining, resume idempotency).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from pycastle.agents.output_protocol import AgentRole, CompletionOutput
from pycastle.config import Config
from pycastle.iteration.improve_candidate_lifecycle import (
    Advance,
    Stop,
    file_and_decide,
    reconcile_and_wind_down,
)
from pycastle.iteration.improve_role_session_store import (
    CandidateItem,
    CandidateList,
    CandidateRecord,
    FiledTicket,
    ImproveRoleSessionStore,
)
from pycastle.iteration.preflight import PreflightAFK, PreflightReady
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.session import RoleSession
from tests.support import FakeAgentRunner, StubPreflightCache, _make_deps

_VALID_BODY = "A" * 120
_STATE_LABEL = "ready-for-agent"


# ---------------------------------------------------------------------------
# Hand-written filing port fake (satisfies FilingPort Protocol)
# ---------------------------------------------------------------------------


class FakeFilingPort:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, list[str]]] = []
        self.closed: list[int] = []
        self.labeled: list[tuple[int, str]] = []
        self.sub_issues: list[tuple[int, int]] = []
        self.dependencies: list[tuple[int, int]] = []
        self._counter = 100

    def create_issue(self, title: str, body: str, labels: list[str]) -> tuple[int, int]:
        number = self._counter
        db_id = self._counter * 10
        self._counter += 1
        self.created.append((title, body, labels))
        return (number, db_id)

    def register_sub_issue(self, parent_number: int, child_database_id: int) -> None:
        self.sub_issues.append((parent_number, child_database_id))

    def add_issue_dependency(self, child_number: int, blocker_database_id: int) -> None:
        self.dependencies.append((child_number, blocker_database_id))

    def apply_label(self, issue_number: int, label: str) -> None:
        self.labeled.append((issue_number, label))

    def close_issue(self, issue_number: int) -> None:
        self.closed.append(issue_number)


# ---------------------------------------------------------------------------
# Draft-writing helpers
# ---------------------------------------------------------------------------


def _write_spec_draft(draft_dir: Path, *, body: str = _VALID_BODY) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "spec.md").write_text(
        f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n  - {_STATE_LABEL}\n---\n\n{body}"
    )


def _write_slice_draft(draft_dir: Path, name: str, *, body: str = _VALID_BODY) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / f"{name}.md").write_text(
        f"---\ntitle: {name} Slice\nlabels:\n  - behavior-slice\n  - {_STATE_LABEL}\n---\n\n{body}"
    )


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_candidate_list(
    role_session_dir: Path,
    candidates: list[CandidateItem],
    *,
    cursor: int = 0,
) -> None:
    role_session_dir.mkdir(parents=True, exist_ok=True)
    store = ImproveRoleSessionStore(role_session_dir)
    store.write_candidate_list(CandidateList(candidates=tuple(candidates)))
    store.write_cursor(cursor)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def role_session_dir(tmp_path: Path) -> Path:
    path = tmp_path / ".pycastle-session" / "improve"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_github_svc(
    *,
    create_side_effect: list[tuple[int, int]] | None = None,
) -> MagicMock:
    svc = MagicMock()
    svc.repo = "test/repo"
    svc.search_open_issues_by_title.return_value = []
    if create_side_effect is not None:
        svc.create_issue_in.side_effect = create_side_effect
    else:
        svc.create_issue_in.return_value = (100, 1000)
    return svc


def _make_test_deps(
    sandbox_path: Path,
    *,
    improve_dispatched_count: int = 0,
    cfg: Config | None = None,
    preflight_cache: StubPreflightCache | None = None,
    agent_runner: FakeAgentRunner | None = None,
    github_svc: MagicMock | None = None,
):
    resolved_github_svc = github_svc if github_svc is not None else _make_github_svc()
    resolved_cfg = cfg if cfg is not None else Config(issue_label=_STATE_LABEL)
    resolved_cache = (
        preflight_cache
        if preflight_cache is not None
        else StubPreflightCache(PreflightReady(sha="abc123"))
    )
    resolved_runner = (
        agent_runner
        if agent_runner is not None
        else FakeAgentRunner(preflight_responses=None)
    )
    deps = _make_deps(
        sandbox_path,
        resolved_runner,
        github_svc=resolved_github_svc,
        cfg=resolved_cfg,
        preflight_cache=resolved_cache,
    )
    return dataclasses.replace(deps, improve_dispatched_count=improve_dispatched_count)


def _run_file_and_decide(
    *,
    role_session_dir: Path,
    sandbox_path: Path,
    deps,
    step_namespace: str = "candidate/0",
    fingerprint: str = "abc123",
    completed_count: int = 0,
) -> Advance | Stop:
    return asyncio.run(
        file_and_decide(
            step_namespace=step_namespace,
            deps=deps,
            role_session_dir=role_session_dir,
            sandbox_path=sandbox_path,
            fingerprint=fingerprint,
            completed_count=completed_count,
        )
    )


# ---------------------------------------------------------------------------
# reconcile_and_wind_down: fingerprint gate
# ---------------------------------------------------------------------------


def test_reconcile_no_op_when_fingerprint_matches(tmp_path: Path) -> None:
    """Matching fingerprint → session preserved, filing port not called."""
    port = FakeFilingPort()
    pre_session = RoleSession(tmp_path, AgentRole.IMPROVE)
    pre_session.start_fresh()
    pre_session.write_fingerprint("sha-abc")

    reconcile_and_wind_down(
        tmp_path,
        fingerprint="sha-abc",
        port=port,  # type: ignore[arg-type]
        cfg=Config(),
    )

    assert pre_session.path.is_dir()
    assert not port.closed
    assert not port.created


def test_reconcile_discards_session_on_fingerprint_mismatch(tmp_path: Path) -> None:
    """Mismatched fingerprint → session dir removed."""
    port = FakeFilingPort()
    pre_session = RoleSession(tmp_path, AgentRole.IMPROVE)
    pre_session.start_fresh()
    pre_session.write_fingerprint("old-sha")

    reconcile_and_wind_down(
        tmp_path,
        fingerprint="new-sha",
        port=port,  # type: ignore[arg-type]
        cfg=Config(),
    )

    assert not pre_session.path.is_dir()


# ---------------------------------------------------------------------------
# reconcile_and_wind_down: AC2 wind-down
# ---------------------------------------------------------------------------


def test_reconcile_ac2_closes_spec_only_candidate(tmp_path: Path) -> None:
    """AC2: spec-only candidate (no slices, not labeled) is closed on fingerprint mismatch."""
    port = FakeFilingPort()
    pre_session = RoleSession(tmp_path, AgentRole.IMPROVE)
    pre_session.start_fresh()
    pre_session.write_fingerprint("old-sha")

    store = ImproveRoleSessionStore(pre_session.path)
    store.write_candidate_list(
        CandidateList(
            candidates=(
                CandidateItem(rank=1, title="First"),
                CandidateItem(rank=2, title="Second"),
            )
        )
    )
    store.write_cursor(1)
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=100,
            spec_database_id=1000,
            spec_title="First Spec",
            filed_tickets=(),
            labels_applied=True,
        ),
    )
    store.write_candidate_record(
        1,
        CandidateRecord(
            spec_number=200,
            spec_database_id=2000,
            spec_title="Second Spec",
            filed_tickets=(),
            labels_applied=False,
        ),
    )

    reconcile_and_wind_down(
        tmp_path,
        fingerprint="new-sha",
        port=port,  # type: ignore[arg-type]
        cfg=Config(),
    )

    assert port.closed == [200]


def test_reconcile_ac2_no_close_when_no_spec(tmp_path: Path) -> None:
    """AC2 boundary: candidate with no spec filed is not touched on fingerprint mismatch."""
    port = FakeFilingPort()
    pre_session = RoleSession(tmp_path, AgentRole.IMPROVE)
    pre_session.start_fresh()
    pre_session.write_fingerprint("old-sha")

    store = ImproveRoleSessionStore(pre_session.path)
    store.write_candidate_list(
        CandidateList(
            candidates=(
                CandidateItem(rank=1, title="First"),
                CandidateItem(rank=2, title="Second"),
            )
        )
    )
    store.write_cursor(1)
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=100,
            spec_database_id=1000,
            spec_title="First Spec",
            filed_tickets=(),
            labels_applied=True,
        ),
    )
    # Candidate 1: no record written → no spec

    reconcile_and_wind_down(
        tmp_path,
        fingerprint="new-sha",
        port=port,  # type: ignore[arg-type]
        cfg=Config(),
    )

    assert not port.closed


# ---------------------------------------------------------------------------
# reconcile_and_wind_down: AC3 wind-down
# ---------------------------------------------------------------------------


def test_reconcile_ac3_completes_partial_slice_candidate(tmp_path: Path) -> None:
    """AC3: partial-slice candidate (spec + slices filed, not labeled) gets labels applied."""
    port = FakeFilingPort()
    pre_session = RoleSession(tmp_path, AgentRole.IMPROVE)
    pre_session.start_fresh()
    pre_session.write_fingerprint("old-sha")

    store = ImproveRoleSessionStore(pre_session.path)
    store.write_candidate_list(
        CandidateList(candidates=(CandidateItem(rank=1, title="First"),))
    )
    store.write_cursor(0)
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=200,
            spec_database_id=2000,
            spec_title="Spec",
            filed_tickets=(
                FiledTicket(
                    handle="slice-a", number=201, database_id=2001, title="Slice A"
                ),
            ),
            labels_applied=False,
        ),
    )

    draft_dir = pre_session.path / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "slice-a")

    reconcile_and_wind_down(
        tmp_path,
        fingerprint="new-sha",
        port=port,  # type: ignore[arg-type]
        cfg=Config(issue_label=_STATE_LABEL),
    )

    labeled_numbers = [num for num, _ in port.labeled]
    assert 201 in labeled_numbers
    assert 200 not in labeled_numbers  # spec must not receive state label
    assert not port.closed


def test_reconcile_ac3_untouched_when_drafts_invalid(tmp_path: Path) -> None:
    """AC3: partial-slice candidate is not modified when draft validation fails."""
    port = FakeFilingPort()
    pre_session = RoleSession(tmp_path, AgentRole.IMPROVE)
    pre_session.start_fresh()
    pre_session.write_fingerprint("old-sha")

    store = ImproveRoleSessionStore(pre_session.path)
    store.write_candidate_list(
        CandidateList(candidates=(CandidateItem(rank=1, title="First"),))
    )
    store.write_cursor(0)
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=100,
            spec_database_id=1000,
            spec_title="Spec",
            filed_tickets=(
                FiledTicket(
                    handle="01-slice", number=101, database_id=1001, title="Slice"
                ),
            ),
            labels_applied=False,
        ),
    )
    # No draft files written → validation will fail

    reconcile_and_wind_down(
        tmp_path,
        fingerprint="new-sha",
        port=port,  # type: ignore[arg-type]
        cfg=Config(issue_label=_STATE_LABEL),
    )

    assert not port.labeled
    assert not port.closed


# ---------------------------------------------------------------------------
# file_and_decide: advance / stop outcomes
# ---------------------------------------------------------------------------


def test_file_and_decide_advance_on_valid_drafts(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Valid drafts → Advance with completed_count incremented by 1."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    _write_spec_draft(role_session_dir / "_drafts")

    result = _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path),
    )

    assert isinstance(result, Advance)
    assert result.completed_count == 1


def test_file_and_decide_stop_cap_reached(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """When dispatched + completed reaches improve_max, Stop(cap-reached) is returned."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    _write_spec_draft(role_session_dir / "_drafts")

    # improve_dispatched_count=0, completed becomes 1 → 0+1 >= 1 → cap reached
    result = _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(
            tmp_path, cfg=Config(issue_label=_STATE_LABEL, improve_max=1)
        ),
    )

    assert isinstance(result, Stop)
    assert result.reason == "cap-reached"
    assert result.completed_count == 1


def test_file_and_decide_stop_safe_sha_changed(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """When mid-run preflight returns a different SHA, Stop(safe-sha-changed) is returned."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    _write_spec_draft(role_session_dir / "_drafts")

    result = _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(
            tmp_path,
            preflight_cache=StubPreflightCache(PreflightReady(sha="changed-sha")),
        ),
        fingerprint="original-sha",
    )

    assert isinstance(result, Stop)
    assert result.reason == "safe-sha-changed"
    assert result.completed_count == 1


def test_file_and_decide_stop_safe_sha_non_preflight_ready(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Non-PreflightReady verdict from preflight → Stop(safe-sha-changed)."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    _write_spec_draft(role_session_dir / "_drafts")

    result = _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(
            tmp_path,
            preflight_cache=StubPreflightCache(
                PreflightAFK(sha="abc123", issue_number=1)
            ),
        ),
    )

    assert isinstance(result, Stop)
    assert result.reason == "safe-sha-changed"


# ---------------------------------------------------------------------------
# file_and_decide: draft correction cap
# ---------------------------------------------------------------------------


def test_file_and_decide_stop_drafts_abandoned_after_correction_cap(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """After 3 failed correction attempts, Stop(drafts-abandoned, completed_count=0) returned."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=None,
    )

    result = _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, agent_runner=runner),
    )

    assert isinstance(result, Stop)
    assert result.reason == "drafts-abandoned"
    assert result.completed_count == 0


def test_file_and_decide_drafts_abandoned_clears_draft_dir(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """After abandonment, the _drafts directory is removed."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=None,
    )

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, agent_runner=runner),
    )

    assert not draft_dir.is_dir() or not any(draft_dir.iterdir())


def test_file_and_decide_correction_reprompts_agent_three_times(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Invalid drafts trigger exactly 3 correction reprompts before abandonment."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=None,
    )

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, agent_runner=runner),
    )

    correction_calls = [
        c
        for c in runner.calls
        if c.prompt.template == PromptTemplate.IMPROVE_DRAFT_CORRECTION
    ]
    assert len(correction_calls) == 3


def test_file_and_decide_correction_valid_on_third_attempt_is_filed(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Drafts that become valid on the 3rd correction attempt are filed; Advance returned."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    correction_count = [0]

    def _side_effect(request):
        if request.prompt.template == PromptTemplate.IMPROVE_DRAFT_CORRECTION:
            correction_count[0] += 1
            if correction_count[0] == 3:
                _write_spec_draft(draft_dir)
                _write_slice_draft(draft_dir, "01-slice")
        return CompletionOutput()

    github_svc = _make_github_svc(create_side_effect=[(100, 1000), (101, 1001)])
    runner = FakeAgentRunner(side_effect=_side_effect, preflight_responses=None)

    result = _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, agent_runner=runner, github_svc=github_svc),
    )

    assert isinstance(result, Advance)
    assert github_svc.create_issue_in.call_count == 2  # spec + 1 slice


# ---------------------------------------------------------------------------
# file_and_decide: prev_spec chaining
# ---------------------------------------------------------------------------


def test_file_and_decide_prev_spec_none_for_candidate_0(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Candidate 0 passes prev_spec=None — no cross-candidate dependency wired."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="First")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice")

    github_svc = _make_github_svc(create_side_effect=[(100, 1000), (101, 1001)])

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        step_namespace="candidate/0",
        deps=_make_test_deps(tmp_path, github_svc=github_svc),
    )

    # No add_issue_dependency call from prev_spec (candidate 0 has no previous spec)
    assert not github_svc.add_issue_dependency.called


def test_file_and_decide_prev_spec_forwarded_for_candidate_n(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Candidate N≥1 forwards (spec_number, spec_database_id) of candidate N-1."""
    store = ImproveRoleSessionStore(role_session_dir)
    store.write_candidate_list(
        CandidateList(
            candidates=(
                CandidateItem(rank=1, title="First"),
                CandidateItem(rank=2, title="Second"),
            )
        )
    )
    store.write_cursor(1)
    # Candidate 0 already filed
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=100,
            spec_database_id=1000,
            spec_title="First Spec",
            filed_tickets=(),
            labels_applied=True,
        ),
    )

    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice")

    github_svc = _make_github_svc(create_side_effect=[(200, 2000), (201, 2001)])

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        step_namespace="candidate/1",
        deps=_make_test_deps(tmp_path, github_svc=github_svc),
    )

    # add_issue_dependency should have been called with prev_spec's db_id (1000)
    dep_calls = github_svc.add_issue_dependency.call_args_list
    blocker_db_ids = [c.args[1] for c in dep_calls]
    assert 1000 in blocker_db_ids


# ---------------------------------------------------------------------------
# file_and_decide: resume-from-partial _candidate_record
# ---------------------------------------------------------------------------


def test_file_and_decide_resume_skips_already_filed_spec(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Resume with spec already filed: spec create_issue call is skipped."""
    store = ImproveRoleSessionStore(role_session_dir)
    store.write_candidate_list(
        CandidateList(candidates=(CandidateItem(rank=1, title="Test"),))
    )
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=100,
            spec_database_id=1000,
            spec_title="Spec Issue",
            filed_tickets=(),
            labels_applied=False,
        ),
    )

    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)

    github_svc = _make_github_svc()

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, github_svc=github_svc),
    )

    # Spec is already filed; no slices to add → no create_issue calls
    assert github_svc.create_issue_in.call_count == 0


def test_file_and_decide_resume_idempotent_when_labels_applied(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Resume with labels_applied=True: no redundant label or create calls; Advance returned."""
    store = ImproveRoleSessionStore(role_session_dir)
    store.write_candidate_list(
        CandidateList(candidates=(CandidateItem(rank=1, title="Test"),))
    )
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=100,
            spec_database_id=1000,
            spec_title="Spec Issue",
            filed_tickets=(
                FiledTicket(
                    handle="01-slice", number=101, database_id=1001, title="Slice"
                ),
            ),
            labels_applied=True,
        ),
    )

    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice")

    github_svc = _make_github_svc()

    result = _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, github_svc=github_svc),
    )

    assert isinstance(result, Advance)
    assert github_svc.create_issue_in.call_count == 0
    assert not github_svc.add_label_to_issue.called


# ---------------------------------------------------------------------------
# file_and_decide: fresh-candidate filing (Stage 1a → Stage 1b → Stage 2)
# ---------------------------------------------------------------------------


def test_file_and_decide_fresh_filing_creates_spec_and_slices(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Fresh candidate: Stage 1a creates spec, Stage 1b creates slice."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice")

    github_svc = _make_github_svc(create_side_effect=[(100, 1000), (101, 1001)])

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, github_svc=github_svc),
    )

    assert github_svc.create_issue_in.call_count == 2  # spec (1a) + slice (1b)


def test_file_and_decide_fresh_filing_labels_slice_not_spec(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """Stage 2 applies state label to slice but not to spec (spec is tracking parent)."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice")

    github_svc = _make_github_svc(create_side_effect=[(100, 1000), (101, 1001)])

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, github_svc=github_svc),
    )

    label_calls = github_svc.add_label_to_issue.call_args_list
    labeled_numbers = {call.args[0] for call in label_calls}
    assert 101 in labeled_numbers  # slice receives state label
    assert 100 not in labeled_numbers  # spec must not receive state label


# ---------------------------------------------------------------------------
# file_and_decide: correction cap — failure report filing
# ---------------------------------------------------------------------------


def test_file_and_decide_drafts_abandoned_files_failure_report(
    tmp_path: Path, role_session_dir: Path
) -> None:
    """After abandonment, an unrepairable-draft-set issue is filed on the tracker."""
    _seed_candidate_list(role_session_dir, [CandidateItem(rank=1, title="Test")])
    draft_dir = role_session_dir / "_drafts"
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    github_svc = _make_github_svc()
    runner = FakeAgentRunner(
        [CompletionOutput(), CompletionOutput(), CompletionOutput()],
        preflight_responses=None,
    )

    _run_file_and_decide(
        role_session_dir=role_session_dir,
        sandbox_path=tmp_path,
        deps=_make_test_deps(tmp_path, agent_runner=runner, github_svc=github_svc),
    )

    assert github_svc.create_issue_in.call_count == 1  # failure report issue filed
