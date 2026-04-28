import os
import re
import sys
import tempfile
from pathlib import Path

from .errors import WorktreeError, WorktreeTimeoutError
from .git_service import GitCommandError, GitService, GitTimeoutError

CONTAINER_PARENT_GIT = "/.pycastle-parent-git"


def create_worktree(
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    git_service: GitService | None = None,
) -> None:
    svc = git_service or GitService()
    try:
        branch_exists = svc.verify_ref_exists(branch, repo_path)

        if worktree_path.exists():
            svc.remove_worktree(repo_path, worktree_path)

        try:
            svc.create_worktree(repo_path, worktree_path, branch)
        except GitCommandError as exc:
            raise WorktreeError(str(exc)) from exc

        has_files = (worktree_path / "pyproject.toml").exists() or (
            worktree_path / "requirements.txt"
        ).exists()
        if not has_files and branch_exists:
            if svc.is_ancestor(branch, repo_path):
                svc.remove_worktree(repo_path, worktree_path)
                try:
                    svc.delete_branch(branch, repo_path)
                except GitCommandError as exc:
                    raise WorktreeError(str(exc)) from exc
                try:
                    svc.create_worktree(repo_path, worktree_path, branch)
                except GitCommandError as exc:
                    raise WorktreeError(str(exc)) from exc
                has_files = (worktree_path / "pyproject.toml").exists() or (
                    worktree_path / "requirements.txt"
                ).exists()

        if not has_files:
            listing = (
                "\n".join(sorted(p.name for p in worktree_path.iterdir()))
                if worktree_path.exists()
                else "(missing)"
            )
            svc.remove_worktree(repo_path, worktree_path)
            raise WorktreeError(
                f"No pyproject.toml or requirements.txt found in worktree {worktree_path}. "
                f"Commit your project files before running agents. "
                f"Worktree contents:\n{listing or '(empty)'}"
            )
    except GitTimeoutError as exc:
        raise WorktreeTimeoutError(str(exc)) from exc


def remove_worktree(
    repo_path: Path,
    worktree_path: Path,
    git_service: GitService | None = None,
) -> None:
    svc = git_service or GitService()
    try:
        svc.remove_worktree(repo_path, worktree_path)
    except GitTimeoutError as exc:
        raise WorktreeTimeoutError(str(exc)) from exc


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
