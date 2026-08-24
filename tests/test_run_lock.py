from __future__ import annotations

import contextlib
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from datetime import UTC

from pycastle import run_lock as run_lock_module
from pycastle.errors import RunAlreadyInProgressError, RunSlotTimeoutError
from pycastle.layout import resolve_layout
from pycastle.run_lock import _locked_project_names, run_slot

# ── helpers ────────────────────────────────────────────────────────────────────


def _layout(tmp_path: Path, repo_name: str = "myproject"):
    repo_root = tmp_path / repo_name
    repo_root.mkdir(exist_ok=True)
    pycastle_home = tmp_path / "pycastle-home"
    return resolve_layout(repo_root=repo_root, pycastle_home=pycastle_home)


@contextmanager
def _hold_lock_in_thread(layout, **slot_kwargs) -> Iterator[threading.Event]:
    """Hold a run slot in a background thread; yield an event to release it."""
    inside = threading.Event()
    release = threading.Event()
    error: list[Exception] = []

    def _worker():
        try:
            with run_slot(layout, **slot_kwargs):
                inside.set()
                release.wait()
        except (RunAlreadyInProgressError, RunSlotTimeoutError) as exc:
            error.append(exc)
            inside.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    inside.wait(timeout=5)
    if error:
        raise error[0]
    try:
        yield release
    finally:
        release.set()
        t.join(timeout=5)


@contextmanager
def _patched(module, name: str, value) -> Iterator[None]:
    """Temporarily swap a module attribute back and forth."""
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


def _ensure_marker(marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.touch()


# ── B1: Layout exposes run-slot paths ─────────────────────────────────────────


def test_layout_exposes_global_run_lock_path(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert layout.global_run_lock_path == layout.pycastle_home / ".run.lock"


def test_layout_exposes_run_markers_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert layout.run_markers_dir == layout.pycastle_home / ".runs"


def test_layout_exposes_project_run_marker_path_with_sanitised_name(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path, repo_name="My Project!")
    assert layout.project_run_marker_path == layout.run_markers_dir / "my-project.lock"


def test_layout_project_run_marker_path_under_run_markers_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path, repo_name="myproject")
    assert layout.project_run_marker_path.parent == layout.run_markers_dir


def test_layout_run_slot_paths_honour_pycastle_home_precedence(tmp_path: Path) -> None:
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    layout = resolve_layout(
        repo_root=tmp_path / "repo",
        pycastle_home=home_a,
        env={"PYCASTLE_HOME": str(home_b)},
    )
    assert layout.global_run_lock_path.is_relative_to(home_a)
    assert layout.run_markers_dir.is_relative_to(home_a)


# ── B2: Lock-respecting acquisition on idle host ───────────────────────────────


def test_lock_respecting_run_acquires_and_releases_on_clean_exit(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with run_slot(layout):
        assert layout.global_run_lock_path.exists()
        assert layout.project_run_marker_path.exists()


def test_lock_respecting_run_releases_both_locks_on_exception(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ValueError, match="boom"), run_slot(layout):
        raise ValueError("boom")

    # After the exception, another run slot can be acquired immediately.
    with run_slot(layout):
        pass


# ── B3: Queue-jumping acquisition ─────────────────────────────────────────────


def test_queue_jumping_run_starts_immediately_when_global_lock_is_held(
    tmp_path: Path,
) -> None:
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    # project-alpha holds the global run lock; project-beta queue-jumps past it.
    # If the queue-jumper incorrectly needed the global lock this would time out.
    with (
        _hold_lock_in_thread(layout_a),
        run_slot(layout_b, ignore_global_lock=True),
    ):
        pass


def test_queue_jumping_run_releases_on_exception(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(RuntimeError), run_slot(layout, ignore_global_lock=True):
        raise RuntimeError("boom")

    with run_slot(layout, ignore_global_lock=True):
        pass


# ── B4: Own marker already held raises RunAlreadyInProgressError ──────────────


def test_lock_respecting_raises_already_in_progress_when_own_marker_held(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with (
        _hold_lock_in_thread(layout, ignore_global_lock=True),
        pytest.raises(RunAlreadyInProgressError, match="myproject"),
        run_slot(layout, timeout=0.1, poll_interval=0.01),
    ):
        pass


def test_queue_jumping_raises_already_in_progress_when_own_marker_held(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with (
        _hold_lock_in_thread(layout, ignore_global_lock=True),
        pytest.raises(RunAlreadyInProgressError),
        run_slot(layout, ignore_global_lock=True),
    ):
        pass


# ── B5: Wait notifications ─────────────────────────────────────────────────────


def test_wait_for_global_lock_emits_begin_and_clear_notifications(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    notifications: list[str] = []
    waiting_started = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        waiting_started.set()

    with _hold_lock_in_thread(layout) as release:
        done = threading.Event()

        def _run():
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting_started.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    assert len(notifications) == 2
    assert any("global run lock" in n.lower() for n in notifications)


def test_wait_for_project_marker_names_the_blocked_project(tmp_path: Path) -> None:
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    notifications: list[str] = []
    waiting_started = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        waiting_started.set()

    with _hold_lock_in_thread(layout_a, ignore_global_lock=True) as release:
        done = threading.Event()

        def _run():
            with run_slot(layout_b, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting_started.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    assert any("project-alpha" in n for n in notifications)
    assert len(notifications) == 2


# ── B6: Lock-respecting waits for other project's marker, then proceeds ────────


def test_lock_respecting_waits_while_other_project_marker_held_then_proceeds(
    tmp_path: Path,
) -> None:
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    proceeded = threading.Event()

    with _hold_lock_in_thread(layout_a, ignore_global_lock=True) as release:

        def _run():
            with run_slot(layout_b, timeout=10, poll_interval=0.01):
                proceeded.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        # Should not proceed while project-alpha's marker is held.
        assert not proceeded.wait(0.1), "run_slot should wait while other marker held"

        release.set()
        assert proceeded.wait(5), "run_slot should proceed once other marker released"
        t.join(timeout=5)


# ── B7: Timeout raises RunSlotTimeoutError ─────────────────────────────────────


def test_timeout_waiting_for_global_lock_raises_run_slot_timeout_error(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    with (
        _hold_lock_in_thread(layout),
        pytest.raises(RunSlotTimeoutError),
        run_slot(layout, timeout=0.05, poll_interval=0.01),
    ):
        pass


def test_timeout_waiting_for_project_markers_raises_run_slot_timeout_error(
    tmp_path: Path,
) -> None:
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    with (
        _hold_lock_in_thread(layout_a, ignore_global_lock=True),
        pytest.raises(RunSlotTimeoutError),
        run_slot(layout_b, timeout=0.05, poll_interval=0.01),
    ):
        pass


# ── B8: Waiting lock-respecting run holds no own marker ───────────────────────


def test_lock_respecting_holds_no_own_marker_while_waiting_for_other_marker(
    tmp_path: Path,
) -> None:
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    waiting = threading.Event()

    def _on_wait(msg: str) -> None:
        waiting.set()

    with _hold_lock_in_thread(layout_a, ignore_global_lock=True) as release:

        def _run():
            try:
                with run_slot(
                    layout_b, timeout=10, poll_interval=0.01, on_wait=_on_wait
                ):
                    pass
            except (RunSlotTimeoutError, RunAlreadyInProgressError):
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting.wait(timeout=5)

        # project-beta is now in its wait loop for project-alpha's marker.
        # If it incorrectly held its own marker, this queue-jumping run would
        # raise RunAlreadyInProgressError. Succeeding proves the marker is free.
        with run_slot(layout_b, ignore_global_lock=True):
            pass

        release.set()
        t.join(timeout=5)


# ── B8a: The marker scan never probes the scanning run's own marker ───────────


def test_marker_scan_skips_the_scanning_projects_own_marker(tmp_path: Path) -> None:
    """`_is_exclusively_locked` takes the marker's lock to probe it.

    Probing our own marker therefore makes B8 ("a waiting run holds no own
    marker") true only probabilistically: a queue-jumper colliding with the
    probe is rejected. The scan must filter by project name *before* probing.
    """
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    probed: list[str] = []
    real_probe = run_lock_module._is_exclusively_locked

    def _spy(path: Path) -> bool:
        probed.append(path.stem)
        return real_probe(path)

    with _hold_lock_in_thread(layout_a, ignore_global_lock=True):
        # Give project-beta a marker file so the glob would otherwise pick it up.
        with run_slot(layout_b, ignore_global_lock=True):
            pass
        probed.clear()
        with _patched(run_lock_module, "_is_exclusively_locked", _spy):
            locked = _locked_project_names(
                layout_b.run_markers_dir, skip="project-beta"
            )

    assert locked == ["project-alpha"]
    assert "project-beta" not in probed
    assert "project-alpha" in probed


def test_queue_jumper_is_not_rejected_by_a_concurrent_marker_scan(
    tmp_path: Path,
) -> None:
    """B8 end to end, made deterministic by widening the probe's lock hold.

    A probe of project-beta's marker holds that marker's lock for as long as it
    takes the probing thread to be rescheduled — normally microseconds, which is
    why the collision only showed up under full-suite CPU contention. Stretching
    that hold turns the race into a certainty, so a regression fails every run
    instead of one run in a thousand.
    """
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    real_probe = run_lock_module._is_exclusively_locked

    def _slow_probe(path: Path) -> bool:
        if path.stem != "project-beta":
            return real_probe(path)
        with path.open("r+b") as fh:
            if not run_lock_module._try_lock(fh.fileno()):
                return True
            time.sleep(0.05)
            run_lock_module._unlock(fh.fileno())
            return False

    waiting = threading.Event()

    with (
        _patched(run_lock_module, "_is_exclusively_locked", _slow_probe),
        _hold_lock_in_thread(layout_a, ignore_global_lock=True) as release,
    ):
        # Give project-beta a marker file so a scan would pick it up.
        with run_slot(layout_b, ignore_global_lock=True):
            pass

        def _run():
            with (
                contextlib.suppress(RunSlotTimeoutError, RunAlreadyInProgressError),
                run_slot(
                    layout_b,
                    timeout=30,
                    poll_interval=0.0,
                    on_wait=lambda _msg: waiting.set(),
                ),
            ):
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert waiting.wait(timeout=5), "project-beta never entered its wait loop"

        # project-beta is parked waiting for project-alpha. Its own marker must
        # be free for every queue-jumper, not just the lucky ones.
        for _ in range(10):
            with run_slot(layout_b, ignore_global_lock=True):
                pass

        release.set()
        t.join(timeout=5)


def test_marker_acquisition_retries_past_a_transient_probe_hold(
    tmp_path: Path,
) -> None:
    """A scan of *another* project's marker must not reject that project's run.

    Filtering our own marker out of the scan does not help the project being
    scanned: its marker is probed, and its own queue-jumper can collide with
    that hold. Real acquisition retries instead of believing the first refusal.
    """
    layout = _layout(tmp_path)
    marker_path = layout.project_run_marker_path
    _ensure_marker(marker_path)

    held = threading.Event()
    released = threading.Event()

    def _hold_briefly() -> None:
        with marker_path.open("r+b") as fh:
            assert run_lock_module._try_lock(fh.fileno())
            held.set()
            time.sleep(0.05)
            run_lock_module._unlock(fh.fileno())
            released.set()

    t = threading.Thread(target=_hold_briefly, daemon=True)
    t.start()
    assert held.wait(timeout=5), "probe holder never took the marker"

    # The marker is held right now, so a single try-lock would refuse it.
    with run_slot(layout, ignore_global_lock=True):
        assert released.is_set()
    t.join(timeout=5)


def test_marker_acquisition_still_reports_a_genuinely_held_marker(
    tmp_path: Path,
) -> None:
    """The retry window must not turn a real same-project overlap into a wait."""
    layout = _layout(tmp_path)

    with _hold_lock_in_thread(layout, ignore_global_lock=True):
        started = time.monotonic()
        with (
            pytest.raises(RunAlreadyInProgressError),
            run_slot(layout, ignore_global_lock=True),
        ):
            pass
        elapsed = time.monotonic() - started

    # Bounded: it aborts, it does not queue behind the holder.
    assert elapsed < 5


# ── B9: Own marker taken by queue-jumping → already-in-progress ───────────────


def test_lock_respecting_reports_already_in_progress_when_queue_jumper_holds_marker(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    with (
        _hold_lock_in_thread(layout, ignore_global_lock=True),
        pytest.raises(RunAlreadyInProgressError),
        run_slot(layout, timeout=0.1, poll_interval=0.01),
    ):
        pass


# ── B11: Run holder record lifecycle ─────────────────────────────────────────


def test_lock_respecting_run_writes_holder_record_on_lock_acquire(
    tmp_path: Path,
) -> None:
    import json
    import os

    layout = _layout(tmp_path)
    with run_slot(layout):
        data = json.loads(layout.run_holder_record_path.read_text())
    assert data["project"] == "myproject"
    assert data["pid"] == os.getpid()
    assert "started_at" in data


def test_lock_respecting_run_removes_holder_record_after_releasing_lock(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with run_slot(layout):
        pass
    assert not layout.run_holder_record_path.exists()


def test_lock_respecting_run_removes_holder_record_on_exception(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(ValueError, match="boom"), run_slot(layout):
        raise ValueError("boom")
    assert not layout.run_holder_record_path.exists()


def test_holder_record_present_while_waiting_for_other_project_markers(
    tmp_path: Path,
) -> None:
    import json

    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    record_while_waiting: dict = {}
    waiting = threading.Event()

    def _on_wait(msg: str) -> None:
        if "project run markers" in msg.lower():
            with contextlib.suppress(OSError, ValueError):
                record_while_waiting.update(
                    json.loads(layout_b.run_holder_record_path.read_text())
                )
            waiting.set()

    with _hold_lock_in_thread(layout_a, ignore_global_lock=True) as release:

        def _run():
            with run_slot(layout_b, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting.wait(timeout=5)
        release.set()
        t.join(timeout=5)

    assert record_while_waiting.get("project") == "project-beta"


# ── B13: Waiting run reports holder info from record ─────────────────────────


def test_waiting_run_reports_holder_project_pid_and_elapsed(tmp_path: Path) -> None:
    import os
    import re

    layout = _layout(tmp_path)
    notifications: list[str] = []
    waiting_started = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        waiting_started.set()

    holder_pid = os.getpid()

    with _hold_lock_in_thread(layout) as release:
        done = threading.Event()

        def _run():
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting_started.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    wait_msg = next(n for n in notifications if "global run lock" in n.lower())
    assert "myproject" in wait_msg
    assert str(holder_pid) in wait_msg
    assert re.search(r"\d+s", wait_msg)


# ── B14: Fallback to cannot-identify-holder ───────────────────────────────────


def test_waiting_run_reports_cannot_identify_when_no_record(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    notifications: list[str] = []
    waiting_started = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        waiting_started.set()

    with _hold_lock_in_thread(layout) as release:
        layout.run_holder_record_path.unlink(missing_ok=True)
        done = threading.Event()

        def _run():
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting_started.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    wait_msg = next(n for n in notifications if "global run lock" in n.lower())
    assert wait_msg == "Waiting for global run lock (cannot identify holder)"


def test_waiting_run_reports_cannot_identify_when_record_is_truncated(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    notifications: list[str] = []
    waiting_started = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        waiting_started.set()

    with _hold_lock_in_thread(layout) as release:
        layout.run_holder_record_path.parent.mkdir(parents=True, exist_ok=True)
        layout.run_holder_record_path.write_text("{truncated", encoding="utf-8")
        done = threading.Event()

        def _run():
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting_started.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    wait_msg = next(n for n in notifications if "global run lock" in n.lower())
    assert wait_msg == "Waiting for global run lock (cannot identify holder)"


def test_waiting_run_reports_cannot_identify_when_record_names_dead_process(
    tmp_path: Path,
) -> None:
    import json as _json
    import subprocess
    import sys
    from datetime import datetime

    layout = _layout(tmp_path)
    notifications: list[str] = []
    waiting_started = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        waiting_started.set()

    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    dead_pid = proc.pid

    with _hold_lock_in_thread(layout) as release:
        layout.run_holder_record_path.parent.mkdir(parents=True, exist_ok=True)
        layout.run_holder_record_path.write_text(
            _json.dumps(
                {
                    "project": "ghost",
                    "pid": dead_pid,
                    "started_at": datetime.now(tz=UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        done = threading.Event()

        def _run():
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting_started.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    wait_msg = next(n for n in notifications if "global run lock" in n.lower())
    assert wait_msg == "Waiting for global run lock (cannot identify holder)"


# ── B15: Timeout error names the recorded holder ─────────────────────────────


def test_timeout_error_names_holder_project_pid_and_started_at(
    tmp_path: Path,
) -> None:
    import os

    layout = _layout(tmp_path)
    holder_pid = os.getpid()

    with (
        _hold_lock_in_thread(layout),
        pytest.raises(RunSlotTimeoutError) as exc_info,
        run_slot(layout, timeout=0.05, poll_interval=0.01),
    ):
        pass

    msg = str(exc_info.value)
    assert "myproject" in msg
    assert str(holder_pid) in msg
    assert "started" in msg.lower()


# ── B16: Timeout without holder keeps current wording ────────────────────────


def test_timeout_without_identifiable_holder_keeps_current_wording(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    with _hold_lock_in_thread(layout) as release:
        layout.run_holder_record_path.unlink(missing_ok=True)
        with (
            pytest.raises(RunSlotTimeoutError) as exc_info,
            run_slot(layout, timeout=0.05, poll_interval=0.01),
        ):
            pass
        release.set()

    assert str(exc_info.value) == "Timed out waiting for global run lock"


# ── B17: Holder change during wait emits fresh notification ──────────────────


def test_holder_change_during_wait_emits_fresh_notification(
    tmp_path: Path,
) -> None:
    import json
    import os
    from datetime import UTC, datetime

    layout = _layout(tmp_path)
    notifications: list[str] = []
    got_first = threading.Event()
    got_second = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        if len(notifications) == 1:
            got_first.set()
        elif len(notifications) == 2 and "global run lock" in msg.lower():
            got_second.set()

    with _hold_lock_in_thread(layout) as release:
        done = threading.Event()

        def _run() -> None:
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        got_first.wait(timeout=5)

        # Overwrite holder record to simulate a different holder identity.
        layout.run_holder_record_path.write_text(
            json.dumps(
                {
                    "project": "different-project",
                    "pid": os.getpid(),
                    "started_at": datetime.now(tz=UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        got_second.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    wait_msgs = [n for n in notifications if "global run lock" in n.lower()]
    assert any("myproject" in m for m in wait_msgs)
    assert any("different-project" in m for m in wait_msgs)


# ── B18: Stable holder produces exactly one holder notification ───────────────


def test_stable_holder_produces_exactly_one_holder_notification(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    notifications: list[str] = []
    waiting_started = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        waiting_started.set()

    with _hold_lock_in_thread(layout) as release:
        done = threading.Event()

        def _run() -> None:
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting_started.wait(timeout=5)
        # Let several polls happen before releasing.
        import time as _time

        _time.sleep(0.05)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    holder_msgs = [
        n
        for n in notifications
        if "global run lock" in n.lower() and "acquired" not in n.lower()
    ]
    assert len(holder_msgs) == 1


# ── B19: Late-written holder record is named without restart ─────────────────


def test_late_written_holder_record_named_without_restart(tmp_path: Path) -> None:
    import json
    import os
    from datetime import UTC, datetime

    layout = _layout(tmp_path)
    notifications: list[str] = []
    got_cannot_identify = threading.Event()
    got_named = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        if "cannot identify" in msg:
            got_cannot_identify.set()
        elif "held by" in msg.lower():
            got_named.set()

    with _hold_lock_in_thread(layout) as release:
        # Remove record so waiter sees "cannot identify" first.
        layout.run_holder_record_path.unlink(missing_ok=True)
        done = threading.Event()

        def _run() -> None:
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        got_cannot_identify.wait(timeout=5)

        # Write the record now (simulating late holder record publication).
        layout.run_holder_record_path.write_text(
            json.dumps(
                {
                    "project": "myproject",
                    "pid": os.getpid(),
                    "started_at": datetime.now(tz=UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        got_named.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    assert got_cannot_identify.is_set()
    assert got_named.is_set()
    named_msgs = [n for n in notifications if "held by" in n.lower()]
    assert any("myproject" in m for m in named_msgs)


# ── B20: Holder disappears mid-wait degrades to cannot-identify ──────────────


def test_holder_disappears_mid_wait_degrades_to_cannot_identify(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    notifications: list[str] = []
    got_named = threading.Event()
    got_cannot_identify = threading.Event()

    def _on_wait(msg: str) -> None:
        notifications.append(msg)
        if "held by" in msg.lower():
            got_named.set()
        elif "cannot identify" in msg:
            got_cannot_identify.set()

    with _hold_lock_in_thread(layout) as release:
        done = threading.Event()

        def _run() -> None:
            with run_slot(layout, timeout=10, poll_interval=0.01, on_wait=_on_wait):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        got_named.wait(timeout=5)

        # Remove the record while the holder still holds the lock.
        layout.run_holder_record_path.unlink(missing_ok=True)
        got_cannot_identify.wait(timeout=5)
        release.set()
        done.wait(timeout=5)
        t.join(timeout=5)

    assert got_named.is_set()
    assert got_cannot_identify.is_set()


# ── B12: Queue-jumping run leaves no holder record ───────────────────────────


def test_queue_jumping_run_leaves_no_holder_record(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with run_slot(layout, ignore_global_lock=True):
        assert not layout.run_holder_record_path.exists()
    assert not layout.run_holder_record_path.exists()


# ── B10: Markers created on first use, never deleted ─────────────────────────


def test_marker_created_on_first_run_and_persists_after_exit(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert not layout.project_run_marker_path.exists()
    with run_slot(layout):
        pass
    assert layout.project_run_marker_path.exists()


def test_global_run_lock_file_created_on_first_run(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    assert not layout.global_run_lock_path.exists()
    with run_slot(layout):
        pass
    assert layout.global_run_lock_path.exists()
