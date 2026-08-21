"""Tests for ImprovePhaseDriver at its three-method interface."""

from pathlib import Path

import pytest

from pycastle.agents.output_protocol import (
    CompletionOutput,
    IssueOutput,
    NoCandidateOutput,
    ScanCandidateItem,
    ScanCandidatesOutput,
)
from pycastle.iteration.improve import ImprovePhaseDriver
from pycastle.iteration.improve_preparation import ImproveCandidate
from pycastle.iteration.improve_role_session_store import (
    CandidateRecord,
    ImproveRoleSessionStore,
)
from pycastle.prompts.pipeline import PromptTemplate


@pytest.fixture
def driver_dir(tmp_path: Path) -> Path:
    return tmp_path / "role-session"


def _make_driver(
    driver_dir: Path, *, no_candidate_report: bool = True
) -> ImprovePhaseDriver:
    return ImprovePhaseDriver(driver_dir, no_candidate_report=no_candidate_report)


def _seed_candidate_list(
    driver_dir: Path,
    candidates: list[ScanCandidateItem],
    *,
    no_candidate: bool = False,
    cursor: int = 0,
) -> None:
    """Pre-seed the candidate list and cursor to simulate a prior scan."""
    store = ImproveRoleSessionStore(driver_dir)
    from pycastle.iteration.improve_role_session_store import (
        CandidateItem,
        CandidateList,
    )

    store.write_candidate_list(
        CandidateList(
            candidates=tuple(
                CandidateItem(rank=c.rank, title=c.title) for c in candidates
            ),
            no_candidate=no_candidate,
        )
    )
    store.write_cursor(cursor)


def _seed_candidate_record(
    driver_dir: Path,
    idx: int,
    *,
    spec_number: int | None = None,
    labels_applied: bool = False,
) -> None:
    """Pre-seed a per-candidate record."""
    driver_dir.mkdir(parents=True, exist_ok=True)
    store = ImproveRoleSessionStore(driver_dir)
    record = CandidateRecord(
        spec_number=spec_number,
        spec_database_id=42 if spec_number is not None else None,
        spec_title="Seeded" if spec_number is not None else "",
        filed_slices=(),
        labels_applied=labels_applied,
    )
    store.write_candidate_record(idx, record)


# ── start() sequence ──────────────────────────────────────────────────────────


def test_fresh_run_start_returns_scan_step(driver_dir: Path) -> None:
    """Fresh run (no candidate list) starts at 01-scan."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "01-scan.md"
    assert step.cfg.template == PromptTemplate.IMPROVE_SCAN


def test_start_returns_none_when_all_candidates_done(driver_dir: Path) -> None:
    """Cursor past end of candidate list → start() returns None (terminal)."""
    _seed_candidate_list(
        driver_dir,
        [ScanCandidateItem(rank=1, title="A")],
        cursor=1,  # past end
    )
    driver = _make_driver(driver_dir)
    assert driver.start() is None


# ── happy path: scan → PRD → Issues ──────────────────────────────────────────


def test_full_sequence_one_candidate(driver_dir: Path) -> None:
    """Happy path: start=scan, record scan, next=PRD, record PRD, next=Issues, record, next=None."""
    driver = _make_driver(driver_dir)

    step1 = driver.start()
    assert step1 is not None
    assert step1.prompt_key == "01-scan.md"
    driver.record_outcome(
        step1, ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="C"),))
    )

    step2 = driver.next()
    assert step2 is not None
    assert step2.prompt_key == "02-prd.md"
    driver.record_outcome(step2, IssueOutput(number=10, labels=[]))

    step3 = driver.next()
    assert step3 is not None
    assert step3.prompt_key == "03-issues.md"
    driver.record_outcome(step3, CompletionOutput())

    assert driver.next() is None


# ── no-candidate paths ────────────────────────────────────────────────────────


def test_no_candidate_with_report_enabled_routes_to_04(driver_dir: Path) -> None:
    """no-candidate scan → 04-report when no_candidate_report=True."""
    driver = _make_driver(driver_dir, no_candidate_report=True)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(step1, NoCandidateOutput())

    step2 = driver.next()
    assert step2 is not None
    assert step2.prompt_key == "04-no-candidate-report.md"
    driver.record_outcome(step2, CompletionOutput())

    assert driver.next() is None


def test_no_candidate_with_report_disabled_is_terminal(driver_dir: Path) -> None:
    """no-candidate scan → terminal when no_candidate_report=False."""
    driver = _make_driver(driver_dir, no_candidate_report=False)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(step1, NoCandidateOutput())

    assert driver.next() is None


# ── terminal states ───────────────────────────────────────────────────────────


def test_terminal_after_all_candidates_cursor_at_end(driver_dir: Path) -> None:
    """Cursor at end of candidate list → immediately terminal."""
    _seed_candidate_list(
        driver_dir,
        [ScanCandidateItem(rank=1, title="A"), ScanCandidateItem(rank=2, title="B")],
        cursor=2,
    )
    driver = _make_driver(driver_dir)
    assert driver.start() is None


def test_terminal_after_no_candidate_report_done(driver_dir: Path) -> None:
    """Resume from no-candidate with report cursor=1 → immediately terminal."""
    _seed_candidate_list(driver_dir, [], no_candidate=True, cursor=1)
    driver = _make_driver(driver_dir)
    assert driver.start() is None


# ── AC 1: Candidate list written at role level after scan ─────────────────────


def test_candidate_list_written_to_role_dir_after_scan(driver_dir: Path) -> None:
    """After scan records candidates, ordered list is durable at role level."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "01-scan.md"

    driver.record_outcome(
        step,
        ScanCandidatesOutput(
            candidates=(
                ScanCandidateItem(rank=1, title="Alpha"),
                ScanCandidateItem(rank=2, title="Beta"),
            )
        ),
    )

    store = ImproveRoleSessionStore(driver_dir)
    candidate_list = store.read_candidate_list()
    assert candidate_list is not None
    assert [c.title for c in candidate_list.candidates] == ["Alpha", "Beta"]


def test_candidate_list_is_at_role_level_not_inside_namespace(driver_dir: Path) -> None:
    """Candidate list lives directly in role_session_dir, not in any namespace subdir."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    driver.record_outcome(
        step, ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="X"),))
    )
    # The list file must be directly at driver_dir, not inside main/ or issues/
    assert (driver_dir / "_candidate_list").is_file()
    assert not (driver_dir / "main" / "_candidate_list").exists()


# ── AC 5: No record → spec (PRD) phase ───────────────────────────────────────


def test_candidate_with_no_record_starts_at_prd(driver_dir: Path) -> None:
    """Candidate with no per-candidate record starts from the spec (PRD) phase."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=1, title="A")])
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "02-prd.md"


# ── AC 3: Existing record → slice (Issues) phase ─────────────────────────────


def test_candidate_with_existing_record_resumes_at_issues(driver_dir: Path) -> None:
    """Candidate whose record exists (PRD done) resumes at the slice (Issues) phase."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=1, title="A")])
    _seed_candidate_record(driver_dir, 0)
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "03-issues.md"


# ── AC 4: Record with spec_number → Issues phase, not scan ───────────────────


def test_candidate_with_spec_number_starts_at_issues_not_scan(driver_dir: Path) -> None:
    """Candidate whose record names a filed spec issue goes to Issues, never back to scan."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=1, title="A")])
    _seed_candidate_record(driver_dir, 0, spec_number=99)
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "03-issues.md"


# ── AC 2: Per-candidate phase state ──────────────────────────────────────────


def test_advancing_one_candidate_leaves_other_unchanged(driver_dir: Path) -> None:
    """Advancing candidate 0 through Issues phase leaves candidate 1 at PRD phase."""
    _seed_candidate_list(
        driver_dir,
        [ScanCandidateItem(rank=1, title="A"), ScanCandidateItem(rank=2, title="B")],
        cursor=0,
    )
    # Candidate 0 has completed Issues (cursor advanced to 1)
    driver_for_candidate_0 = _make_driver(driver_dir)
    step1 = driver_for_candidate_0.start()
    assert step1 is not None
    assert step1.prompt_key == "02-prd.md"
    driver_for_candidate_0.record_outcome(step1, IssueOutput(number=1, labels=[]))

    step2 = driver_for_candidate_0.next()
    assert step2 is not None
    assert step2.prompt_key == "03-issues.md"
    driver_for_candidate_0.record_outcome(step2, CompletionOutput())

    # Now cursor is at 1. Candidate 1 has no record → PRD phase.
    step3 = driver_for_candidate_0.next()
    assert step3 is not None
    assert step3.prompt_key == "02-prd.md"

    # Candidate 0's record (written by driver) is separate from candidate 1.
    store = ImproveRoleSessionStore(driver_dir)
    assert store.read_candidate_record(1) is None


# ── AC 6: All candidates complete → terminal ─────────────────────────────────


def test_all_candidates_complete_makes_no_further_dispatch(driver_dir: Path) -> None:
    """When all candidates have labels_applied=True, start() is terminal."""
    _seed_candidate_list(
        driver_dir,
        [ScanCandidateItem(rank=1, title="A"), ScanCandidateItem(rank=2, title="B")],
        cursor=0,
    )
    # Both candidates are fully complete.
    _seed_candidate_record(driver_dir, 0, spec_number=10, labels_applied=True)
    _seed_candidate_record(driver_dir, 1, spec_number=20, labels_applied=True)

    driver = _make_driver(driver_dir)
    assert driver.start() is None


def test_cursor_at_end_of_list_is_terminal(driver_dir: Path) -> None:
    """Cursor past last candidate index → start() returns None with no dispatch."""
    _seed_candidate_list(
        driver_dir,
        [ScanCandidateItem(rank=1, title="A")],
        cursor=1,  # Past end of single-element list.
    )
    driver = _make_driver(driver_dir)
    assert driver.start() is None


# ── send_role_prompt_on_resume ────────────────────────────────────────────────


def test_cold_start_scan_does_not_send_role_prompt(driver_dir: Path) -> None:
    """Cold start: scan step has send_role_prompt_on_resume=False."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.send_role_prompt_on_resume is False


def test_prd_step_sends_role_prompt_after_scan(driver_dir: Path) -> None:
    """PRD step after successful scan signals send_role_prompt_on_resume=True."""
    driver = _make_driver(driver_dir)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(
        step1, ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="A"),))
    )
    step2 = driver.next()
    assert step2 is not None
    assert step2.prompt_key == "02-prd.md"
    assert step2.send_role_prompt_on_resume is True


def test_mid_prd_retry_does_not_send_role_prompt(driver_dir: Path) -> None:
    """In-flight=02-prd → PRD step has send_role_prompt_on_resume=False (mid-phase retry)."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=1, title="A")])
    store = ImproveRoleSessionStore(driver_dir)
    store.write_in_flight("02-prd")
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "02-prd.md"
    assert step.send_role_prompt_on_resume is False


def test_clean_prd_entry_sends_role_prompt(driver_dir: Path) -> None:
    """No in-flight at PRD start → send_role_prompt_on_resume=True (cross-teardown resume)."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=1, title="A")])
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "02-prd.md"
    assert step.send_role_prompt_on_resume is True


# ── in-flight marker written before step is consumed ─────────────────────────


def test_start_writes_in_flight_before_returning(driver_dir: Path) -> None:
    """start() writes the in-flight marker to disk before returning the step."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None

    store = ImproveRoleSessionStore(driver_dir)
    assert store.read_in_flight() == "01-scan"


def test_next_writes_in_flight_before_returning(driver_dir: Path) -> None:
    """next() writes the in-flight marker before returning the step."""
    driver = _make_driver(driver_dir)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(
        step1, ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="A"),))
    )

    step2 = driver.next()
    assert step2 is not None

    store = ImproveRoleSessionStore(driver_dir)
    assert store.read_in_flight() == "02-prd"


# ── record_outcome disk effects ───────────────────────────────────────────────


def test_record_outcome_clears_in_flight_after_scan(driver_dir: Path) -> None:
    """record_outcome for scan clears the in-flight marker."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None

    driver.record_outcome(
        step, ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="A"),))
    )

    store = ImproveRoleSessionStore(driver_dir)
    assert store.read_in_flight() is None


def test_record_outcome_advances_cursor_after_issues(driver_dir: Path) -> None:
    """record_outcome for Issues increments cursor on disk."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=1, title="A")])
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "02-prd.md"
    driver.record_outcome(step, IssueOutput(number=1, labels=[]))

    step2 = driver.next()
    assert step2 is not None
    assert step2.prompt_key == "03-issues.md"
    driver.record_outcome(step2, CompletionOutput())

    store = ImproveRoleSessionStore(driver_dir)
    assert store.read_cursor() == 1


# ── ImproveCandidate threading ────────────────────────────────────────────────


def test_scan_step_has_no_candidate(driver_dir: Path) -> None:
    """Scan step is not candidate-scoped, so candidate is None."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "01-scan.md"
    assert step.candidate is None


def test_prd_step_carries_candidate_with_rank_title_and_no_spec_number(
    driver_dir: Path,
) -> None:
    """PRD step carries the candidate identity from the scan; spec_number is absent before filing."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=3, title="Foo Bar")])
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "02-prd.md"
    assert step.candidate == ImproveCandidate(rank=3, title="Foo Bar", spec_number=None)


def test_issues_step_carries_candidate_with_no_spec_number_before_filing(
    driver_dir: Path,
) -> None:
    """Issues step has spec_number=None when the candidate record has no spec yet."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=2, title="Alpha")])
    _seed_candidate_record(driver_dir, 0)  # record with spec_number=None
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "03-issues.md"
    assert step.candidate == ImproveCandidate(rank=2, title="Alpha", spec_number=None)


def test_issues_step_carries_spec_number_once_filing_has_produced_one(
    driver_dir: Path,
) -> None:
    """Issues step carries a real spec_number when filing has already produced one (resume after partial filing)."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=1, title="Beta")])
    _seed_candidate_record(driver_dir, 0, spec_number=42)
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "03-issues.md"
    assert step.candidate == ImproveCandidate(rank=1, title="Beta", spec_number=42)


def test_candidate_built_from_durable_scan_output_not_rederived(
    driver_dir: Path,
) -> None:
    """Candidate identity (rank, title) comes from the durable candidate list, not the live scan output."""
    _seed_candidate_list(
        driver_dir,
        [ScanCandidateItem(rank=5, title="Persisted Title")],
    )
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "02-prd.md"
    assert step.candidate is not None
    assert step.candidate.rank == 5
    assert step.candidate.title == "Persisted Title"


def test_mid_issues_resume_preserves_candidate(driver_dir: Path) -> None:
    """Mid-issues resume keeps candidate intact when overriding send_role_prompt_on_resume."""
    _seed_candidate_list(driver_dir, [ScanCandidateItem(rank=7, title="Resume Me")])
    _seed_candidate_record(driver_dir, 0, spec_number=99)
    store = ImproveRoleSessionStore(driver_dir)
    store.write_in_flight("03-issues")
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "03-issues.md"
    assert step.send_role_prompt_on_resume is False
    assert step.candidate == ImproveCandidate(rank=7, title="Resume Me", spec_number=99)
