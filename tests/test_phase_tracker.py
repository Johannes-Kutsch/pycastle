"""Direct tests for _PhaseTracker: all assertions through the tracker's own interface."""

from pathlib import Path

import pytest

from pycastle.iteration.improve import _PhaseTracker


@pytest.fixture
def tracker(tmp_path: Path) -> _PhaseTracker:
    return _PhaseTracker(tmp_path / "role-session")


def test_load_with_no_files_returns_no_state(tracker: _PhaseTracker) -> None:
    """No files on disk → (None, None)."""
    assert tracker.load() == (None, None)


def test_load_with_valid_progress_only(tracker: _PhaseTracker) -> None:
    """Valid progress, no in-flight → (id, None)."""
    tracker.record_in_flight("01-scan")
    tracker.record_completed("01-scan:picked")
    assert tracker.load() == ("01-scan:picked", None)


def test_load_with_valid_progress_and_in_flight(tracker: _PhaseTracker) -> None:
    """Valid progress and in-flight → (progress_id, in_flight_id)."""
    tracker.record_in_flight("01-scan")
    tracker.record_completed("01-scan:picked")
    tracker.record_in_flight("02-prd")
    assert tracker.load() == ("01-scan:picked", "02-prd")


def test_orphan_reset_returns_no_state(tracker: _PhaseTracker) -> None:
    """progress=02-prd without in-flight=03-issues → (None, None) on load."""
    tracker.record_in_flight("02-prd")
    tracker.record_completed("02-prd")
    # No record_in_flight("03-issues") — simulates orphan crash
    assert tracker.load() == (None, None)


def test_orphan_reset_subsequent_load_still_no_state(tracker: _PhaseTracker) -> None:
    """Orphan-reset clears progress so a second load also returns no state."""
    tracker.record_in_flight("02-prd")
    tracker.record_completed("02-prd")
    tracker.load()  # triggers orphan-reset
    assert tracker.load() == (None, None)


def test_load_with_02_prd_and_03_issues_in_flight_is_not_orphan(
    tracker: _PhaseTracker,
) -> None:
    """progress=02-prd WITH in-flight=03-issues is a valid mid-phase resume, not an orphan."""
    tracker.record_in_flight("02-prd")
    tracker.record_completed("02-prd")
    tracker.record_in_flight("03-issues")
    assert tracker.load() == ("02-prd", "03-issues")


def test_malformed_progress_fails_soft_to_no_state(
    tmp_path: Path,
) -> None:
    """Malformed (unrecognised) progress ID fails soft to (None, None)."""
    role_session_dir = tmp_path / "role-session"
    role_session_dir.mkdir()
    tracker = _PhaseTracker(role_session_dir)
    # Seed a malformed value via a completed-id that isn't in the valid set
    tracker.record_in_flight("01-scan")
    # Bypass record_completed to write a bad value directly
    (role_session_dir / "_phase_progress").write_text("corrupted!", encoding="utf-8")
    (role_session_dir / "_phase_in_flight").unlink(missing_ok=True)
    assert tracker.load() == (None, None)


def test_record_in_flight_makes_load_see_in_flight_id(tracker: _PhaseTracker) -> None:
    """After record_in_flight, load returns the in-flight id."""
    tracker.record_in_flight("02-prd")
    _, in_flight_id = tracker.load()
    assert in_flight_id == "02-prd"


def test_record_completed_makes_load_see_completed_id_with_in_flight_cleared(
    tracker: _PhaseTracker,
) -> None:
    """After record_completed, load returns the completed id with in-flight=None."""
    tracker.record_in_flight("03-issues")
    tracker.record_completed("03-issues")
    last_id, in_flight_id = tracker.load()
    assert last_id == "03-issues"
    assert in_flight_id is None


def test_all_valid_phase_ids_are_recognized(tracker: _PhaseTracker) -> None:
    """Each non-orphan phase id round-trips through record_completed → load."""
    # 02-prd is excluded: without in-flight=03-issues it always triggers orphan-reset
    valid_ids = ["01-scan:picked", "01-scan:no-candidate", "03-issues", "04-report"]
    for phase_id in valid_ids:
        tracker.record_in_flight("some-phase")
        tracker.record_completed(phase_id)
        last_id, _ = tracker.load()
        assert last_id == phase_id, f"Expected {phase_id!r}, got {last_id!r}"


def test_record_in_flight_creates_parent_dirs(tmp_path: Path) -> None:
    """record_in_flight creates the directory if it doesn't exist yet."""
    deep_dir = tmp_path / "a" / "b" / "c"
    tracker = _PhaseTracker(deep_dir)
    tracker.record_in_flight("01-scan")
    _, in_flight_id = tracker.load()
    assert in_flight_id == "01-scan"


def test_record_completed_creates_parent_dirs(tmp_path: Path) -> None:
    """record_completed creates the directory if it doesn't exist yet."""
    deep_dir = tmp_path / "x" / "y" / "z"
    tracker = _PhaseTracker(deep_dir)
    tracker.record_completed("03-issues")
    last_id, _ = tracker.load()
    assert last_id == "03-issues"
