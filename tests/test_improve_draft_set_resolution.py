"""Tests for improve_draft_set_resolution module interface.

Covers: resolve_draft_set outcome space — Ready on first success, Ready after
one correction, Unrepairable after exhausting all correction attempts, snapshot
behaviour (non-.md excluded, OSError suppressed).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from pycastle.iteration.improve_drafts import DraftSetValidationError

from pycastle.config import Config
from pycastle.iteration.improve_draft_set_resolution import (
    _MAX_CORRECTION_ATTEMPTS,
    Ready,
    Unrepairable,
    resolve_draft_set,
)
from tests.support import _write_slice_draft, _write_spec_draft

_VALID_BODY = "A" * 120


def _draft_dir(tmp_path: Path) -> Path:
    d = tmp_path / "_drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingCallback:
    def __init__(self) -> None:
        self.calls: list[tuple[DraftSetValidationError, int]] = []

    async def __call__(self, exc: DraftSetValidationError, attempt: int) -> None:
        self.calls.append((exc, attempt))


# ---------------------------------------------------------------------------
# First read succeeds
# ---------------------------------------------------------------------------


def test_first_read_succeeds_returns_ready_no_callback(tmp_path: Path) -> None:
    """If the first read_draft_set succeeds, outcome is Ready and callback is never invoked."""
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)

    cb = _RecordingCallback()
    outcome = _run(
        resolve_draft_set(draft_dir=draft_dir, cfg=Config(), correction_callback=cb)
    )

    assert isinstance(outcome, Ready)
    assert len(outcome.drafts) == 1
    assert not cb.calls


# ---------------------------------------------------------------------------
# One correction fixes the drafts
# ---------------------------------------------------------------------------


def test_one_correction_returns_ready_callback_invoked_once(tmp_path: Path) -> None:
    """After one failed read + one correction, a successful read returns Ready."""
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    cb = _RecordingCallback()

    async def _fixing_callback(exc: DraftSetValidationError, attempt: int) -> None:
        await cb(exc, attempt)
        # Fix the invalid slice on the first callback
        _write_slice_draft(draft_dir, "01-slice")

    outcome = _run(
        resolve_draft_set(
            draft_dir=draft_dir, cfg=Config(), correction_callback=_fixing_callback
        )
    )

    assert isinstance(outcome, Ready)
    assert len(outcome.drafts) == 2
    assert len(cb.calls) == 1
    assert cb.calls[0][1] == 0  # attempt index 0


# ---------------------------------------------------------------------------
# Repeated failures exhaust correction attempts
# ---------------------------------------------------------------------------


def test_repeated_failures_returns_unrepairable_callback_invoked_limit_times(
    tmp_path: Path,
) -> None:
    """After _MAX_CORRECTION_ATTEMPTS corrections without success, outcome is Unrepairable."""
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    cb = _RecordingCallback()
    outcome = _run(
        resolve_draft_set(draft_dir=draft_dir, cfg=Config(), correction_callback=cb)
    )

    assert isinstance(outcome, Unrepairable)
    assert len(cb.calls) == _MAX_CORRECTION_ATTEMPTS
    assert outcome.problems  # non-empty problem list from last read


def test_unrepairable_carries_last_problems(tmp_path: Path) -> None:
    """Unrepairable.problems matches DraftSetValidationError.problems from the last read."""
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    outcome = _run(
        resolve_draft_set(
            draft_dir=draft_dir, cfg=Config(), correction_callback=_RecordingCallback()
        )
    )

    assert isinstance(outcome, Unrepairable)
    assert any("body" in p.lower() or "short" in p.lower() for p in outcome.problems)


def test_unrepairable_snapshot_contains_md_files(tmp_path: Path) -> None:
    """Unrepairable.draft_files snapshot contains keyed .md filenames."""
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    outcome = _run(
        resolve_draft_set(
            draft_dir=draft_dir, cfg=Config(), correction_callback=_RecordingCallback()
        )
    )

    assert isinstance(outcome, Unrepairable)
    assert "spec.md" in outcome.draft_files
    assert "01-slice.md" in outcome.draft_files


def test_unrepairable_snapshot_excludes_non_md_files(tmp_path: Path) -> None:
    """Non-.md files in the drafts directory are excluded from the snapshot."""
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")
    (draft_dir / "notes.txt").write_text("ignored")

    outcome = _run(
        resolve_draft_set(
            draft_dir=draft_dir, cfg=Config(), correction_callback=_RecordingCallback()
        )
    )

    assert isinstance(outcome, Unrepairable)
    assert "notes.txt" not in outcome.draft_files


def test_unrepairable_snapshot_suppresses_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-file OSError during snapshot read is suppressed; other files still included.

    The correction loop reads each .md file (_MAX_CORRECTION_ATTEMPTS + 1) times.
    We make the file raise OSError only after those reads so that the loop completes
    successfully (yielding Unrepairable) while the snapshot call is the one that fails.
    """
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    original_read_text = type(draft_dir / "01-slice.md").read_text
    call_count: list[int] = [0]
    loop_reads = _MAX_CORRECTION_ATTEMPTS + 1  # 4 reads during the correction loop

    def _read_text_with_late_failure(self, encoding: str = "utf-8") -> str:
        if self.name == "01-slice.md":
            call_count[0] += 1
            if call_count[0] > loop_reads:
                raise OSError("simulated snapshot read error")
        return original_read_text(self, encoding=encoding)

    monkeypatch.setattr(
        type(draft_dir / "01-slice.md"),
        "read_text",
        _read_text_with_late_failure,
    )

    outcome = _run(
        resolve_draft_set(
            draft_dir=draft_dir, cfg=Config(), correction_callback=_RecordingCallback()
        )
    )

    assert isinstance(outcome, Unrepairable)
    # 01-slice.md read failed during snapshot → absent; spec.md was readable
    assert "spec.md" in outcome.draft_files
    assert "01-slice.md" not in outcome.draft_files


# ---------------------------------------------------------------------------
# Callback invoked between reads only
# ---------------------------------------------------------------------------


def test_callback_attempt_indices_are_zero_based_and_sequential(
    tmp_path: Path,
) -> None:
    """Callback is invoked with attempt indices 0, 1, 2 in order."""
    draft_dir = _draft_dir(tmp_path)
    _write_spec_draft(draft_dir)
    _write_slice_draft(draft_dir, "01-slice", body="Too short.")

    cb = _RecordingCallback()
    _run(resolve_draft_set(draft_dir=draft_dir, cfg=Config(), correction_callback=cb))

    assert [attempt for _, attempt in cb.calls] == list(range(_MAX_CORRECTION_ATTEMPTS))
