import subprocess

import pytest


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with one commit, ready for worktree operations."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (tmp_path / "README.md").write_text("# Test")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return tmp_path
