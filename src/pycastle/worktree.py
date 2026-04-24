import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar

from .errors import WorktreeError

_T = TypeVar("_T")


def _retry_on_permission_error(fn: Callable[[], _T], attempts: int = 3, delay: float = 0.1) -> _T:
    for attempt in range(attempts):
        try:
            return fn()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _is_ancestor(branch: str, repo_path: Path) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, "HEAD"],
        cwd=repo_path, capture_output=True,
    )
    return result.returncode == 0


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo_path, capture_output=True,
    )

    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path, capture_output=True,
        )
        shutil.rmtree(worktree_path, ignore_errors=True)

    rev_parse = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo_path, capture_output=True,
    )

    if rev_parse.returncode == 0:
        cmd = ["git", "worktree", "add", str(worktree_path), branch]
    else:
        cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"]

    result = subprocess.run(cmd, cwd=repo_path, capture_output=True)
    if result.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
        )

    has_files = (
        (worktree_path / "pyproject.toml").exists()
        or (worktree_path / "requirements.txt").exists()
    )
    if not has_files and rev_parse.returncode == 0:
        if _is_ancestor(branch, repo_path):
            remove_worktree(repo_path, worktree_path)
            subprocess.run(["git", "branch", "-D", branch], cwd=repo_path, capture_output=True)
            result = subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
                cwd=repo_path, capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
                )
            has_files = (
                (worktree_path / "pyproject.toml").exists()
                or (worktree_path / "requirements.txt").exists()
            )

    if not has_files:
        listing = (
            "\n".join(sorted(p.name for p in worktree_path.iterdir()))
            if worktree_path.exists()
            else "(missing)"
        )
        remove_worktree(repo_path, worktree_path)
        raise WorktreeError(
            f"No pyproject.toml or requirements.txt found in worktree {worktree_path}. "
            f"Commit your project files before running agents. "
            f"Worktree contents:\n{listing or '(empty)'}"
        )


def remove_worktree(repo_path: Path, worktree_path: Path) -> None:
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path, capture_output=True,
    )
    if result.returncode != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)


def patch_gitdir_for_container(worktree_path: Path) -> None:
    """Rewrite the worktree .git file to use the container-internal repo path.

    Only needed on Windows where git writes a Windows-style absolute path that
    the Linux container cannot follow.
    """
    if sys.platform != "win32":
        return

    git_file = worktree_path / ".git"
    if not _retry_on_permission_error(git_file.is_file):
        return

    content = _retry_on_permission_error(lambda: git_file.read_text(encoding="utf-8"))

    def _rewrite(m: re.Match) -> str:
        path = m.group(1).strip().replace("\\", "/")
        idx = path.find(".git/worktrees/")
        if idx == -1:
            return m.group(0)
        suffix = path[idx:]  # ".git/worktrees/<name>"
        return f"gitdir: /home/agent/repo/{suffix}"

    new_content = re.sub(r"gitdir:\s*(.+)", _rewrite, content)
    _retry_on_permission_error(lambda: git_file.write_text(new_content.rstrip() + "\n", encoding="utf-8"))
