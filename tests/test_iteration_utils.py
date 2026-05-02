import asyncio
from unittest.mock import MagicMock

from pycastle.config import Config
from pycastle.services import GitService, GithubService
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    NullStatusDisplay,
    RecordingLogger,
    RecordingStatusDisplay,
)
from pycastle.iteration._utils import _wait_for_clean_working_tree


def _make_deps(tmp_path, git_svc, *, status_display=None):
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=MagicMock(spec=GithubService),
        agent_runner=FakeAgentRunner([]),
        cfg=Config(),
        logger=RecordingLogger(),
        status_display=status_display or NullStatusDisplay(),
    )


def _run(coro):
    return asyncio.run(coro)


# ── Clean tree ────────────────────────────────────────────────────────────────


def test_returns_immediately_when_tree_is_clean(tmp_path):
    git_svc = MagicMock(spec=GitService)
    git_svc.is_working_tree_clean.return_value = True
    deps = _make_deps(tmp_path, git_svc)
    _run(_wait_for_clean_working_tree(deps))
    git_svc.is_working_tree_clean.assert_called_once_with(tmp_path)


# ── Dirty tree ────────────────────────────────────────────────────────────────


def test_prints_red_message_when_tree_is_dirty(tmp_path):
    git_svc = MagicMock(spec=GitService)
    git_svc.is_working_tree_clean.side_effect = [False, True]
    recording = RecordingStatusDisplay()
    deps = _make_deps(tmp_path, git_svc, status_display=recording)
    _run(_wait_for_clean_working_tree(deps))
    print_messages = [msg for kind, msg, *_ in recording.calls if kind == "print"]
    assert any("Working tree" in msg for msg in print_messages)
    dirty_msg = next((msg for msg in print_messages if "Working tree" in msg), None)
    assert dirty_msg is not None
    assert dirty_msg.startswith("[red]")
    assert dirty_msg.endswith("[/red]")


def test_polls_until_tree_is_clean(tmp_path):
    git_svc = MagicMock(spec=GitService)
    git_svc.is_working_tree_clean.side_effect = [False, False, True]
    deps = _make_deps(tmp_path, git_svc)
    _run(_wait_for_clean_working_tree(deps))
    assert git_svc.is_working_tree_clean.call_count == 3
