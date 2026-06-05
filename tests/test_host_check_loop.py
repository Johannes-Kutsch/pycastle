import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from pycastle._host_check import HostCheckPassedVerdict, run_host_check_loop
from tests.support import RecordingStatusDisplay


def test_run_host_check_loop_surfaces_aggregate_phase_row_before_returning_verdict(
    tmp_path: Path,
) -> None:
    events: list[tuple[object, ...]] = []
    git_svc = MagicMock()

    def fake_pull(repo_root: Path) -> None:
        events.append(("pull", repo_root))

    def fake_clean(repo_root: Path) -> bool:
        events.append(("clean", repo_root))
        return True

    git_svc.pull_with_merge_fallback.side_effect = fake_pull
    git_svc.is_working_tree_clean.side_effect = fake_clean
    git_svc.get_head_sha.return_value = "abc123def456"

    class _TransientWorktree:
        async def __aenter__(self) -> Path:
            events.append(("worktree-enter",))
            return tmp_path

        async def __aexit__(self, exc_type, exc, tb) -> None:
            events.append(("worktree-exit",))
            return None

    def fake_run_host_check(name: str, command: str, cwd: Path) -> None:
        events.append(("host-check", name, command, cwd))
        return None

    display = RecordingStatusDisplay()

    result = asyncio.run(
        run_host_check_loop(
            host_checks=(("tests", "python -c tests"),),
            git_svc=git_svc,
            repo_root=tmp_path,
            status_display=display,
            run_host_check=fake_run_host_check,
            transient_worktree_factory=lambda *a, **kw: _TransientWorktree(),
        )
    )

    assert result == HostCheckPassedVerdict(checked_sha="abc123def456")
    assert display.calls[0] == (
        "register",
        "Host Check",
        "phase",
        "started",
        "Setup",
        None,
    )
    assert events[:3] == [
        ("pull", tmp_path),
        ("clean", tmp_path),
        ("worktree-enter",),
    ]
    assert ("host-check", "tests", "python -c tests", tmp_path) in events
    assert display.calls[-1] == ("remove", "Host Check", "finished", "success")
