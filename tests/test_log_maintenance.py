import os
import time
from pathlib import Path

import pytest

from pycastle.log_maintenance import maintain_logs


@pytest.fixture()
def logs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


# ── Behavior 1: trim log files exceeding max_lines ────────────────────────────


def test_log_file_exceeding_max_lines_is_trimmed(logs_dir: Path) -> None:
    log = logs_dir / "app.log"
    log.write_text("\n".join(str(i) for i in range(200)))
    _set_fresh_mtime(log)

    maintain_logs(logs_dir, max_lines=100, retention_days=30)

    lines = log.read_text().splitlines()
    assert len(lines) == 100
    assert lines[0] == "100"
    assert lines[-1] == "199"


def test_log_file_within_max_lines_is_unchanged(logs_dir: Path) -> None:
    content = "\n".join(str(i) for i in range(50))
    log = logs_dir / "app.log"
    log.write_text(content)
    _set_fresh_mtime(log)

    maintain_logs(logs_dir, max_lines=100, retention_days=30)

    assert log.read_text() == content


# ── Behavior 2: sweep *.log files older than retention_days ───────────────────


def _set_old_mtime(path: Path, days: int = 31) -> None:
    t = time.time() - days * 24 * 3600
    os.utime(path, (t, t))


def _set_fresh_mtime(path: Path) -> None:
    # Pin mtime to the (frozen) clock: the suite clock is fixed while file
    # mtimes come from the real filesystem clock, so files that must survive
    # the retention sweep state their age explicitly.
    t = time.time()
    os.utime(path, (t, t))


def test_old_log_files_are_deleted(logs_dir: Path) -> None:
    old = logs_dir / "old.log"
    old.write_text("stale")
    _set_old_mtime(old)

    maintain_logs(logs_dir, max_lines=10000, retention_days=30)

    assert not old.exists()


def test_recent_log_files_are_kept(logs_dir: Path) -> None:
    recent = logs_dir / "recent.log"
    recent.write_text("fresh")
    _set_fresh_mtime(recent)

    maintain_logs(logs_dir, max_lines=10000, retention_days=30)

    assert recent.exists()


def test_log_file_with_non_utf8_bytes_does_not_crash(logs_dir: Path) -> None:
    log = logs_dir / "binary.log"
    log.write_bytes(b"line1\nsome \x9d bad \xff bytes\nline3\n")
    _set_fresh_mtime(log)

    maintain_logs(logs_dir, max_lines=10000, retention_days=30)

    assert log.exists()


def test_non_log_files_are_untouched(logs_dir: Path) -> None:
    txt = logs_dir / "notes.txt"
    txt.write_text("not a log")
    _set_old_mtime(txt)

    maintain_logs(logs_dir, max_lines=10000, retention_days=30)

    assert txt.exists()
