import os
import pathlib
import subprocess

import pytest


def pytest_configure():
    os.chdir(pathlib.Path(__file__).parent.parent)


@pytest.fixture(autouse=True)
def _hermetic_pycastle_home(monkeypatch, tmp_path_factory):
    monkeypatch.setenv(
        "PYCASTLE_HOME", str(tmp_path_factory.mktemp("pycastle_home_isolated"))
    )


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
