from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pycastle.errors import RunAlreadyInProgressError, RunSlotTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from pycastle.layout import PycastleLayout

__all__ = ["run_slot"]

_DEFAULT_TIMEOUT: float = 6 * 3600
_DEFAULT_POLL: float = 1.0


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        else:
            return True
    else:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        else:
            return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _is_exclusively_locked(path: Path) -> bool:
    try:
        with path.open("r+b") as fh:
            if _try_lock(fh.fileno()):
                _unlock(fh.fileno())
                return False
            return True
    except OSError:
        return False


def _locked_project_names(markers_dir: Path) -> list[str]:
    return [
        p.stem for p in sorted(markers_dir.glob("*.lock")) if _is_exclusively_locked(p)
    ]


def _ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()


def _notify(on_wait: Callable[[str], None] | None, message: str) -> None:
    if on_wait is not None:
        on_wait(message)


def _waiting_message(record_path: Path) -> str:
    default = "Waiting for global run lock (cannot identify holder)"
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        project = str(data["project"])
        pid = int(data["pid"])
        started_at = datetime.fromisoformat(data["started_at"])
    except (OSError, KeyError, ValueError, TypeError):
        return default
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return default
    elapsed = int((datetime.now(tz=UTC) - started_at).total_seconds())
    return (
        f"Waiting for global run lock held by {project}"
        f" (pid {pid}, started {started_at.isoformat()}, {elapsed}s ago)"
    )


def _write_holder_record(record_path: Path, project: str, started_at: str) -> None:
    record = json.dumps(
        {"project": project, "pid": os.getpid(), "started_at": started_at}
    )
    tmp = record_path.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(record, encoding="utf-8")
    tmp.replace(record_path)


def _remove_holder_record(record_path: Path) -> None:
    with contextlib.suppress(OSError):
        record_path.unlink()


@contextlib.contextmanager
def _queue_jumping_slot(marker_path: Path, project_name: str) -> Iterator[None]:
    with marker_path.open("r+b") as marker_fh:
        if not _try_lock(marker_fh.fileno()):
            raise RunAlreadyInProgressError(project_name)
        try:
            yield
        finally:
            _unlock(marker_fh.fileno())


def _wait_for_other_markers(
    markers_dir: Path,
    marker_path: Path,
    project_name: str,
    *,
    deadline: float,
    poll_interval: float,
    on_wait: Callable[[str], None] | None,
) -> None:
    def _other_locked() -> list[str]:
        return [n for n in _locked_project_names(markers_dir) if n != project_name]

    if _is_exclusively_locked(marker_path):
        raise RunAlreadyInProgressError(project_name)

    locked = _other_locked()
    if not locked:
        return
    projects = ", ".join(locked)
    _notify(on_wait, f"Waiting for project run markers: {projects}")
    while True:
        if _is_exclusively_locked(marker_path):
            raise RunAlreadyInProgressError(project_name)
        locked = _other_locked()
        if not locked:
            break
        if time.monotonic() >= deadline:
            raise RunSlotTimeoutError(
                f"Timed out waiting for project run markers: {', '.join(locked)}"
            )
        time.sleep(poll_interval)
    _notify(on_wait, "All project run markers are free")


@contextlib.contextmanager
def run_slot(
    layout: PycastleLayout,
    *,
    ignore_global_lock: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
    poll_interval: float = _DEFAULT_POLL,
    on_wait: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Acquire a run slot (global run lock + project run marker) for the with-block."""
    markers_dir = layout.run_markers_dir
    markers_dir.mkdir(parents=True, exist_ok=True)

    marker_path = layout.project_run_marker_path
    _ensure_file(marker_path)

    project_name = marker_path.stem
    deadline = time.monotonic() + timeout

    if ignore_global_lock:
        # Queue-jumping: take only own project marker.
        with _queue_jumping_slot(marker_path, project_name):
            yield
        return

    # Lock-respecting: token → scan all markers → own marker.
    global_lock_path = layout.global_run_lock_path
    record_path = layout.run_holder_record_path
    _ensure_file(global_lock_path)

    with global_lock_path.open("r+b") as global_fh:
        # Step 1: acquire global run lock.
        if not _try_lock(global_fh.fileno()):
            _notify(on_wait, _waiting_message(record_path))
            while not _try_lock(global_fh.fileno()):
                if time.monotonic() >= deadline:
                    raise RunSlotTimeoutError("Timed out waiting for global run lock")
                time.sleep(poll_interval)
            _notify(on_wait, "Global run lock acquired")

        started_at = datetime.now(tz=UTC).isoformat()
        _write_holder_record(record_path, project_name, started_at)
        try:
            # Step 2: wait until all *other* project markers are free.
            _wait_for_other_markers(
                markers_dir,
                marker_path,
                project_name,
                deadline=deadline,
                poll_interval=poll_interval,
                on_wait=on_wait,
            )

            # Step 3: take own project marker.
            with marker_path.open("r+b") as marker_fh:
                if not _try_lock(marker_fh.fileno()):
                    raise RunAlreadyInProgressError(project_name)
                try:
                    yield
                finally:
                    _unlock(marker_fh.fileno())
        finally:
            _remove_holder_record(record_path)
