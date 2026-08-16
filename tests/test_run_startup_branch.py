"""Tests for branch setup applied at run startup."""

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import click
import pytest

from pycastle.iteration.orchestrator import RunOptions, run
from pycastle.services import GithubService
from tests.support import (
    FakeAgentRunner,
    RecordingStatusDisplay,
    functional_git_svc,
)


def _write_config(tmp_path: Path, **kwargs) -> None:
    (tmp_path / "pycastle").mkdir(exist_ok=True)
    lines = ["from pycastle import StageOverride"]
    for k, v in kwargs.items():
        lines.append(f"{k} = {v!r}")
    (tmp_path / "pycastle" / "config.py").write_text("\n".join(lines) + "\n")


def _make_github_svc():
    mock = MagicMock(spec=GithubService)
    mock.get_open_issues.return_value = []
    mock.get_all_open_issues_lightweight.return_value = []
    mock.check_auth.return_value = "testuser"
    mock.repo = "test/repo"
    return mock


def _make_git_svc(
    *,
    dev_branch: str = "main",
    working_branch: str | None = "feature-x",
    dev_on_origin: bool = True,
    working_on_local: bool = False,
    working_on_origin: bool = False,
    clean: bool = True,
) -> Any:
    git_svc = cast("Any", functional_git_svc())
    git_svc.get_head_sha.return_value = "abc1234"
    git_svc.get_branch_sha.return_value = "abc1234"
    git_svc.try_merge.return_value = True
    git_svc.is_ancestor.return_value = True
    git_svc.is_working_tree_clean.return_value = clean

    def _verify_ref(ref: str, repo_path: Path) -> bool:
        if ref == f"refs/remotes/origin/{dev_branch}":
            return dev_on_origin
        if working_branch and ref == working_branch:
            return working_on_local
        if working_branch and ref == f"refs/remotes/origin/{working_branch}":
            return working_on_origin
        return False

    git_svc.verify_ref_exists.side_effect = _verify_ref
    return git_svc


def _do_run(
    tmp_path: Path,
    git_svc: Any,
    *,
    status_display: Any = None,
    **config_kwargs: object,
) -> None:
    config_kwargs.setdefault("max_iterations", 1)
    config_kwargs.setdefault("max_parallel", 1)
    _write_config(tmp_path, **config_kwargs)
    asyncio.run(
        run(
            {},
            tmp_path,
            RunOptions(
                agent_runner=FakeAgentRunner(),
                git_service=git_svc,
                github_service=_make_github_svc(),
                status_display=status_display,
            ),
        )
    )


# ── Behavior 1: startup never checks out any branch ──────────────────────────


def test_startup_does_not_checkout_any_branch(tmp_path: Path) -> None:
    """Starting a run must never call checkout_branch — the repo root belongs to the operator."""
    git_svc = _make_git_svc()

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.checkout_branch.assert_not_called()


def test_startup_does_not_checkout_when_no_working_branch(tmp_path: Path) -> None:
    """Without a working_branch, run must also never checkout the dev branch."""
    git_svc = _make_git_svc(working_branch=None)

    _do_run(tmp_path, git_svc, dev_branch="main", working_branch=None)

    git_svc.checkout_branch.assert_not_called()


def test_startup_does_not_checkout_existing_working_branch(tmp_path: Path) -> None:
    """When working_branch already exists, run must not checkout it."""
    git_svc = _make_git_svc(working_on_local=True)

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.checkout_branch.assert_not_called()


# ── Behavior 2: new working branch created from origin/dev ────────────────────


def test_new_working_branch_triggers_fetch(tmp_path: Path) -> None:
    """When working_branch does not exist, run must fetch before creating it."""
    git_svc = _make_git_svc()

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.fetch.assert_called()


def test_new_working_branch_created_from_origin_dev(tmp_path: Path) -> None:
    """When working_branch does not exist, run must create it from origin/<dev>."""
    git_svc = _make_git_svc()

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.create_branch_from.assert_called_with(tmp_path, "feature-x", "origin/main")


def test_new_working_branch_is_pushed_upstream(tmp_path: Path) -> None:
    """When working_branch does not exist, run must push it with upstream set."""
    git_svc = _make_git_svc()

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.push_upstream.assert_called_with(tmp_path, "feature-x")


# ── Behavior 3: existing working branch reused without reseeding ──────────────


def test_existing_local_working_branch_not_reseeded(tmp_path: Path) -> None:
    """When working_branch already exists locally, run must not create it from dev."""
    git_svc = _make_git_svc(working_on_local=True)

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.create_branch_from.assert_not_called()


def test_existing_local_working_branch_not_pushed_upstream(tmp_path: Path) -> None:
    """When working_branch already exists locally, run must not push it."""
    git_svc = _make_git_svc(working_on_local=True)

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.push_upstream.assert_not_called()


def test_existing_remote_working_branch_reused_without_seed(tmp_path: Path) -> None:
    """When working_branch already exists on origin, run must not reseed it."""
    git_svc = _make_git_svc(working_on_origin=True)

    _do_run(tmp_path, git_svc, working_branch="feature-x", dev_branch="main")

    git_svc.create_branch_from.assert_not_called()
    git_svc.push_upstream.assert_not_called()


# ── Behavior 4: missing dev branch aborts before agent work ──────────────────


def test_missing_dev_branch_raises_usage_error(tmp_path: Path) -> None:
    """When configured dev branch is absent on origin, run must raise UsageError."""
    git_svc = _make_git_svc(dev_on_origin=False)

    with pytest.raises(click.UsageError):
        _do_run(tmp_path, git_svc, dev_branch="main", working_branch=None)


def test_missing_dev_branch_error_names_branch(tmp_path: Path) -> None:
    """The abort error must name the missing dev branch."""
    git_svc = _make_git_svc(
        dev_branch="release-3", dev_on_origin=False, working_branch=None
    )

    with pytest.raises(click.UsageError) as exc_info:
        _do_run(tmp_path, git_svc, dev_branch="release-3", working_branch=None)

    assert "release-3" in str(exc_info.value)


def test_missing_dev_branch_no_agent_work(tmp_path: Path) -> None:
    """When dev branch is absent, no agent work must start."""
    git_svc = _make_git_svc(dev_on_origin=False)
    agent_runner = FakeAgentRunner()

    _write_config(
        tmp_path,
        max_iterations=1,
        max_parallel=1,
        dev_branch="main",
        working_branch=None,
    )
    with pytest.raises(click.UsageError):
        asyncio.run(
            run(
                {},
                tmp_path,
                RunOptions(
                    agent_runner=agent_runner,
                    git_service=git_svc,
                    github_service=_make_github_svc(),
                ),
            )
        )

    assert agent_runner.calls == []


# ── Behavior 5: no working_branch → anchored to dev branch ───────────────────


def test_no_working_branch_does_not_create_branch(tmp_path: Path) -> None:
    """When working_branch is unset, run must not create any branch."""
    git_svc = _make_git_svc(working_branch=None)

    _do_run(tmp_path, git_svc, dev_branch="main", working_branch=None)

    git_svc.create_branch_from.assert_not_called()


def test_no_working_branch_does_not_push_upstream(tmp_path: Path) -> None:
    """When working_branch is unset, run must not push upstream."""
    git_svc = _make_git_svc(working_branch=None)

    _do_run(tmp_path, git_svc, dev_branch="main", working_branch=None)

    git_svc.push_upstream.assert_not_called()


# ── Issue 2081: resolved branches are announced at startup ────────────────────


def _printed_lines(display: RecordingStatusDisplay) -> list[str]:
    return [str(call[2]) for call in display.calls if call[0] == "print"]


def test_startup_announces_dev_and_working_branch(tmp_path: Path) -> None:
    """The resolved branches must be visible, so an inherited global value is obvious."""
    display = RecordingStatusDisplay()
    git_svc = _make_git_svc()

    _do_run(
        tmp_path,
        git_svc,
        status_display=display,
        dev_branch="main",
        working_branch="feature-x",
    )

    assert "Branches: dev=main, working=feature-x" in _printed_lines(display)


def test_startup_announcement_omits_working_when_unset(tmp_path: Path) -> None:
    display = RecordingStatusDisplay()
    git_svc = _make_git_svc(working_branch=None)

    _do_run(
        tmp_path,
        git_svc,
        status_display=display,
        dev_branch="main",
        working_branch=None,
    )

    assert "Branches: dev=main" in _printed_lines(display)


def test_startup_announces_branches_before_aborting_on_missing_dev(
    tmp_path: Path,
) -> None:
    display = RecordingStatusDisplay()
    git_svc = _make_git_svc(dev_on_origin=False)

    with pytest.raises(click.UsageError):
        _do_run(
            tmp_path,
            git_svc,
            status_display=display,
            dev_branch="main",
            working_branch="feature-x",
        )

    assert "Branches: dev=main, working=feature-x" in _printed_lines(display)


def test_startup_announces_branches_before_github_auth(tmp_path: Path) -> None:
    """Announced before any step that can exit first, so the line is truly unconditional."""
    display = RecordingStatusDisplay()
    git_svc = _make_git_svc()

    _do_run(
        tmp_path,
        git_svc,
        status_display=display,
        dev_branch="main",
        working_branch="feature-x",
    )

    lines = _printed_lines(display)
    branches = lines.index("Branches: dev=main, working=feature-x")
    auth = next(i for i, line in enumerate(lines) if line.startswith("GitHub auth:"))
    assert branches < auth
