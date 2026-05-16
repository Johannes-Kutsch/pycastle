"""Tests for ImprovePhaseDriver at its three-method interface."""

from pathlib import Path

import pytest

from pycastle.agent_output_protocol import (
    CompletionOutput,
    IssueOutput,
    NoCandidateOutput,
)
from pycastle.iteration.improve import ImprovePhaseDriver
from pycastle.prompt_pipeline import PromptTemplate


@pytest.fixture
def driver_dir(tmp_path: Path) -> Path:
    return tmp_path / "role-session"


def _make_driver(
    driver_dir: Path, *, no_candidate_report: bool = True, short_sid: str = "abcd1234"
) -> ImprovePhaseDriver:
    return ImprovePhaseDriver(driver_dir, short_sid, no_candidate_report)


# ── start() sequence ──────────────────────────────────────────────────────────


def test_fresh_run_start_returns_scan_step(driver_dir: Path) -> None:
    """Fresh run (no progress file) starts at 01-scan."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.prompt_key == "01-scan.md"
    assert step.cfg.template == PromptTemplate.IMPROVE_SCAN


def test_fresh_run_start_returns_none_after_terminal(driver_dir: Path) -> None:
    """Terminal state (03-issues completed) → start() returns None."""
    (driver_dir).mkdir(parents=True, exist_ok=True)
    (driver_dir / "_phase_progress").write_text("03-issues", encoding="utf-8")
    driver = _make_driver(driver_dir)
    assert driver.start() is None


# ── happy path: picked → 02-prd → 03-issues ──────────────────────────────────


def test_picked_path_full_sequence(driver_dir: Path) -> None:
    """Picked path: start=01-scan, record picked, next=02-prd, record, next=03-issues, record, next=None."""
    driver = _make_driver(driver_dir)

    step1 = driver.start()
    assert step1 is not None and step1.prompt_key == "01-scan.md"
    driver.record_outcome(step1, CompletionOutput())

    step2 = driver.next()
    assert step2 is not None and step2.prompt_key == "02-prd.md"
    driver.record_outcome(step2, CompletionOutput())

    step3 = driver.next()
    assert step3 is not None and step3.prompt_key == "03-issues.md"
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
    assert step2 is not None and step2.prompt_key == "04-no-candidate-report.md"
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


def test_terminal_after_03_issues(driver_dir: Path) -> None:
    """Resume from 03-issues is immediately terminal."""
    driver_dir.mkdir(parents=True, exist_ok=True)
    (driver_dir / "_phase_progress").write_text("03-issues", encoding="utf-8")
    driver = _make_driver(driver_dir)
    assert driver.start() is None


def test_terminal_after_04_report(driver_dir: Path) -> None:
    """Resume from 04-report is immediately terminal."""
    driver_dir.mkdir(parents=True, exist_ok=True)
    (driver_dir / "_phase_progress").write_text("04-report", encoding="utf-8")
    driver = _make_driver(driver_dir)
    assert driver.start() is None


# ── orphan-after-02 reset ─────────────────────────────────────────────────────


def test_orphan_reset_wipes_progress_and_restarts_at_01(driver_dir: Path) -> None:
    """progress=02-prd without in-flight=03-issues → wipe progress, restart at phase 01."""
    driver_dir.mkdir(parents=True, exist_ok=True)
    progress_file = driver_dir / "_phase_progress"
    progress_file.write_text("02-prd", encoding="utf-8")

    driver = _make_driver(driver_dir)
    step = driver.start()

    assert step is not None and step.prompt_key == "01-scan.md"
    assert not progress_file.exists()


def test_orphan_reset_does_not_trigger_when_03_issues_in_flight(
    driver_dir: Path,
) -> None:
    """progress=02-prd WITH in-flight=03-issues is a valid mid-phase resume, not orphan."""
    driver_dir.mkdir(parents=True, exist_ok=True)
    (driver_dir / "_phase_progress").write_text("02-prd", encoding="utf-8")
    (driver_dir / "_phase_in_flight").write_text("03-issues", encoding="utf-8")

    driver = _make_driver(driver_dir)
    step = driver.start()

    assert step is not None and step.prompt_key == "03-issues.md"


# ── send_role_prompt_on_resume ────────────────────────────────────────────────


def test_cold_start_phase_01_does_not_send_role_prompt(driver_dir: Path) -> None:
    """Cold start: phase 01 step has send_role_prompt_on_resume=False."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None
    assert step.send_role_prompt_on_resume is False


def test_mid_phase_retry_does_not_send_role_prompt(driver_dir: Path) -> None:
    """In-flight marker matches upcoming phase → send_role_prompt_on_resume=False."""
    driver_dir.mkdir(parents=True, exist_ok=True)
    (driver_dir / "_phase_progress").write_text("01-scan:picked", encoding="utf-8")
    (driver_dir / "_phase_in_flight").write_text("02-prd", encoding="utf-8")

    driver = _make_driver(driver_dir)
    step = driver.start()

    assert step is not None and step.prompt_key == "02-prd.md"
    assert step.send_role_prompt_on_resume is False


def test_clean_phase_boundary_sends_role_prompt(driver_dir: Path) -> None:
    """Previous phase completed cleanly, no in-flight → send_role_prompt_on_resume=True."""
    driver_dir.mkdir(parents=True, exist_ok=True)
    (driver_dir / "_phase_progress").write_text("01-scan:picked", encoding="utf-8")

    driver = _make_driver(driver_dir)
    step = driver.start()

    assert step is not None and step.prompt_key == "02-prd.md"
    assert step.send_role_prompt_on_resume is True


def test_next_step_after_record_sends_role_prompt(driver_dir: Path) -> None:
    """Step returned by next() after record_outcome has send_role_prompt_on_resume=True."""
    driver = _make_driver(driver_dir)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(step1, CompletionOutput())

    step2 = driver.next()
    assert step2 is not None and step2.prompt_key == "02-prd.md"
    assert step2.send_role_prompt_on_resume is True


# ── in-flight marker written before step is consumed ─────────────────────────


def test_start_writes_in_flight_before_returning(driver_dir: Path) -> None:
    """start() writes the in-flight marker to disk before returning the step."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None

    in_flight_file = driver_dir / "_phase_in_flight"
    assert in_flight_file.exists()
    assert in_flight_file.read_text(encoding="utf-8") == "01-scan"


def test_next_writes_in_flight_before_returning(driver_dir: Path) -> None:
    """next() writes the in-flight marker before returning the step."""
    driver = _make_driver(driver_dir)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(step1, CompletionOutput())

    step2 = driver.next()
    assert step2 is not None

    in_flight_file = driver_dir / "_phase_in_flight"
    assert in_flight_file.exists()
    assert in_flight_file.read_text(encoding="utf-8") == "02-prd"


# ── record_outcome disk effects ───────────────────────────────────────────────


def test_record_outcome_writes_progress_and_clears_in_flight(driver_dir: Path) -> None:
    """record_outcome writes _phase_progress and removes _phase_in_flight."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None

    driver.record_outcome(step, CompletionOutput())

    progress_file = driver_dir / "_phase_progress"
    in_flight_file = driver_dir / "_phase_in_flight"
    assert progress_file.read_text(encoding="utf-8") == "01-scan:picked"
    assert not in_flight_file.exists()


def test_record_outcome_writes_no_candidate_progress(driver_dir: Path) -> None:
    """record_outcome writes '01-scan:no-candidate' for NoCandidateOutput from scan."""
    driver = _make_driver(driver_dir)
    step = driver.start()
    assert step is not None

    driver.record_outcome(step, NoCandidateOutput())

    progress_file = driver_dir / "_phase_progress"
    assert progress_file.read_text(encoding="utf-8") == "01-scan:no-candidate"


# ── prd_number exposure ───────────────────────────────────────────────────────


def test_prd_number_none_before_phase_02(driver_dir: Path) -> None:
    """prd_number is None before phase 02 outcome is recorded."""
    driver = _make_driver(driver_dir)
    driver.start()
    assert driver.prd_number is None


def test_prd_number_set_from_phase_02_issue_output(driver_dir: Path) -> None:
    """Phase 02 IssueOutput sets prd_number for use by phase 03."""
    driver = _make_driver(driver_dir)

    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(step1, CompletionOutput())

    step2 = driver.next()
    assert step2 is not None and step2.prompt_key == "02-prd.md"
    driver.record_outcome(step2, IssueOutput(number=4242, labels=[]))

    assert driver.prd_number == 4242
