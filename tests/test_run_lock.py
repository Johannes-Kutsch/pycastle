from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from pycastle.errors import RunAlreadyInProgressError, RunSlotTimeoutError
from pycastle.layout import resolve_layout
from pycastle.run_lock import run_slot

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
        except Exception as exc:
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


def test_queue_jumping_run_holds_only_project_marker_not_global_lock(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)

    with _hold_lock_in_thread(layout, ignore_global_lock=True):
        # While queue-jumping run holds its marker, another lock-respecting run
        # can still acquire the global run lock (just not the same marker).
        # Verify this by checking the global lock is NOT held by the queue-jumper.
        # We do so by trying to acquire the global lock independently.
        if sys.platform == "win32":
            import msvcrt

            layout.global_run_lock_path.parent.mkdir(parents=True, exist_ok=True)
            layout.global_run_lock_path.touch(exist_ok=True)
            with open(layout.global_run_lock_path, "r+b") as fh:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    global_free = True
                except OSError:
                    global_free = False
        else:
            import fcntl

            layout.global_run_lock_path.parent.mkdir(parents=True, exist_ok=True)
            layout.global_run_lock_path.touch(exist_ok=True)
            with open(layout.global_run_lock_path, "r+b") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    global_free = True
                except BlockingIOError:
                    global_free = False

        assert global_free, "Queue-jumping run must not hold the global run lock"


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

    with _hold_lock_in_thread(layout) as release:
        # Another lock-respecting run must wait for global lock.
        done = threading.Event()

        def _run():
            with run_slot(
                layout,
                timeout=10,
                poll_interval=0.01,
                on_wait=notifications.append,
            ):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.05)
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

    with _hold_lock_in_thread(layout_a, ignore_global_lock=True) as release:
        done = threading.Event()

        def _run():
            with run_slot(
                layout_b,
                timeout=10,
                poll_interval=0.01,
                on_wait=notifications.append,
            ):
                pass
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.05)
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


def test_lock_respecting_holds_no_own_marker_while_waiting(tmp_path: Path) -> None:
    layout_a = _layout(tmp_path, repo_name="project-alpha")
    layout_b = resolve_layout(
        repo_root=tmp_path / "project-beta",
        pycastle_home=tmp_path / "pycastle-home",
    )
    (tmp_path / "project-beta").mkdir(exist_ok=True)

    waiting = threading.Event()
    notifications: list[str] = []

    def _on_wait(msg: str):
        notifications.append(msg)
        waiting.set()

    with _hold_lock_in_thread(layout_a, ignore_global_lock=True) as release:

        def _run():
            try:
                with run_slot(
                    layout_b,
                    timeout=10,
                    poll_interval=0.01,
                    on_wait=_on_wait,
                ):
                    pass
            except RunSlotTimeoutError:
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        waiting.wait(timeout=5)

        # While project-beta is waiting, its marker must NOT be locked.
        marker_b = layout_b.project_run_marker_path
        if marker_b.exists():
            if sys.platform == "win32":
                import msvcrt

                with open(marker_b, "r+b") as fh:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        marker_free = True
                    except OSError:
                        marker_free = False
            else:
                import fcntl

                with open(marker_b, "r+b") as fh:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(fh, fcntl.LOCK_UN)
                        marker_free = True
                    except BlockingIOError:
                        marker_free = False
            assert marker_free, "Waiting run must not hold its own project marker"

        release.set()
        t.join(timeout=5)


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
