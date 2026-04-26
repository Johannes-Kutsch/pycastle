import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .defaults.config import WORKTREE_TIMEOUT
from .errors import WorktreeError, WorktreeTimeoutError

CONTAINER_PARENT_GIT = "/.pycastle-parent-git"


def _run(*args, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("timeout", WORKTREE_TIMEOUT)
    try:
        return subprocess.run(*args, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise WorktreeTimeoutError(
            f"git command timed out after {WORKTREE_TIMEOUT}s: {exc.cmd}"
        ) from exc


def _is_ancestor(branch: str, repo_path: Path) -> bool:
    result = _run(
        ["git", "merge-base", "--is-ancestor", branch, "HEAD"],
        cwd=repo_path,
        capture_output=True,
    )
    return result.returncode == 0


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> None:
    _run(
        ["git", "worktree", "prune"],
        cwd=repo_path,
        capture_output=True,
    )

    if worktree_path.exists():
        _run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
            capture_output=True,
        )
        shutil.rmtree(worktree_path, ignore_errors=True)

    rev_parse = _run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo_path,
        capture_output=True,
    )

    if rev_parse.returncode == 0:
        cmd = ["git", "worktree", "add", str(worktree_path), branch]
    else:
        cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"]

    result = _run(cmd, cwd=repo_path, capture_output=True)
    if result.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
        )

    has_files = (worktree_path / "pyproject.toml").exists() or (
        worktree_path / "requirements.txt"
    ).exists()
    if not has_files and rev_parse.returncode == 0:
        if _is_ancestor(branch, repo_path):
            remove_worktree(repo_path, worktree_path)
            _run(["git", "branch", "-D", branch], cwd=repo_path, capture_output=True)
            result = _run(
                ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
                cwd=repo_path,
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed: {result.stderr.decode('utf-8', errors='replace').strip()}"
                )
            has_files = (worktree_path / "pyproject.toml").exists() or (
                worktree_path / "requirements.txt"
            ).exists()

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
    result = _run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_path,
        capture_output=True,
    )
    if result.returncode != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)


def patch_gitdir_for_container(worktree_path: Path) -> Path | None:
    """Return a temp file with the container-internal gitdir path, or None.

    Only needed on Windows where git writes a Windows-style absolute path that
    the Linux container cannot follow. The host .git file is never modified;
    the caller should bind-mount the returned path over the container's .git.
    """
    if sys.platform != "win32":
        return None

    git_file = worktree_path / ".git"
    if not git_file.is_file():
        return None

    content = git_file.read_text(encoding="utf-8")

    def _rewrite(m: re.Match) -> str:
        path = m.group(1).strip().replace("\\", "/")
        idx = path.find(".git/worktrees/")
        if idx == -1:
            return m.group(0)
        suffix = path[idx + len(".git/worktrees/") :]  # "<name>"
        return f"gitdir: {CONTAINER_PARENT_GIT}/worktrees/{suffix}"

    new_content = re.sub(r"gitdir:\s*(.+)", _rewrite, content)

    fd, tmp = tempfile.mkstemp(suffix=".gitdir_overlay")
    try:
        os.close(fd)
        Path(tmp).write_text(new_content.rstrip() + "\n", encoding="utf-8")
    except Exception:
        os.unlink(tmp)
        raise
    return Path(tmp)
