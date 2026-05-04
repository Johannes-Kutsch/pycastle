import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pycastle.iteration._utils import _wait_for_clean_working_tree


class _MinimalDeps:
    """Minimal object satisfying _UtilDeps — does NOT extend Deps."""

    def __init__(self, git_svc, repo_root: Path, status_display) -> None:
        self.git_svc = git_svc
        self.repo_root = repo_root
        self.status_display = status_display


def _make_deps(tmp_path: Path, *, clean: bool = True) -> _MinimalDeps:
    git_svc = MagicMock()
    git_svc.is_working_tree_clean.return_value = clean
    return _MinimalDeps(git_svc=git_svc, repo_root=tmp_path, status_display=MagicMock())


def test_util_deps_protocol_satisfied_by_minimal_object(tmp_path):
    """_wait_for_clean_working_tree accepts any object with the three required fields, not just Deps."""
    deps = _make_deps(tmp_path, clean=True)
    asyncio.run(_wait_for_clean_working_tree(deps, "Test"))


def test_returns_immediately_when_tree_is_clean(tmp_path):
    deps = _make_deps(tmp_path, clean=True)
    asyncio.run(_wait_for_clean_working_tree(deps, "Test"))
    deps.status_display.print.assert_not_called()


def test_waits_then_proceeds_when_tree_becomes_clean(tmp_path):
    deps = _make_deps(tmp_path)
    deps.git_svc.is_working_tree_clean.side_effect = [False, True]

    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        asyncio.run(_wait_for_clean_working_tree(deps, "Preflight"))

    deps.status_display.print.assert_called_once()
    call_args = deps.status_display.print.call_args
    assert call_args[0][0] == "Preflight"
    assert "uncommitted changes" in call_args[0][1]
    assert call_args[1]["style"] == "error"
