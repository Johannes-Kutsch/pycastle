import os
import pathlib
import subprocess

import pytest
import time_machine


@pytest.fixture(autouse=True)
def _frozen_clock():
    """Pin the wall clock so no test verdict depends on the real date.

    The instant is fixed in the past: file mtimes come from the real
    filesystem clock, so anything written during a test reads as *future*
    (conservatively fresh) instead of stale enough to trip retention sweeps.
    Tests that care about age set mtimes explicitly relative to the frozen
    clock. tick=True lets time advance within a test, so timeout/deadline
    loops still make progress. Tests needing a specific instant travel to it
    or inject ``now=`` explicitly.
    """
    with time_machine.travel("2020-01-02 03:04:05 +0000", tick=True):
        yield


def pytest_configure():
    os.chdir(pathlib.Path(__file__).parent.parent)


@pytest.fixture(autouse=True)
def _hermetic_pycastle_home(monkeypatch, tmp_path_factory):
    monkeypatch.setenv(
        "PYCASTLE_HOME", str(tmp_path_factory.mktemp("pycastle_home_isolated"))
    )


@pytest.fixture(autouse=True)
def _hermetic_terminal_color_env(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture(autouse=True)
def _no_gh_token(monkeypatch):
    """Guard against tests accidentally hitting the real GitHub API.

    Tests that need a token must set it explicitly via monkeypatch.
    """
    monkeypatch.delenv("GH_TOKEN", raising=False)


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with one commit and a local bare remote, ready for worktree operations."""
    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    repo.mkdir()

    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Test")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "fetch", "origin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "branch", "--set-upstream-to=origin/main", "main"],
        check=True,
        capture_output=True,
    )
    return repo
