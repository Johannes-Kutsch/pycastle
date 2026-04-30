import os
import re
import sys
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from .config import Config, load_config
from .errors import WorktreeError, WorktreeTimeoutError
from .git_service import GitCommandError, GitService, GitTimeoutError

CONTAINER_PARENT_GIT = "/.pycastle-parent-git"


@contextmanager
def _wrap_git_errors():
    try:
        yield
    except GitTimeoutError as exc:
        raise WorktreeTimeoutError(str(exc)) from exc
    except GitCommandError as exc:
        raise WorktreeError(str(exc)) from exc


def _has_project_files(path: Path) -> bool:
    return (path / "pyproject.toml").exists() or (path / "requirements.txt").exists()


def _missing_files_error(path: Path) -> WorktreeError:
    listing = (
        "\n".join(sorted(p.name for p in path.iterdir()))
        if path.exists()
        else "(missing)"
    )
    return WorktreeError(
        f"No pyproject.toml or requirements.txt found in worktree {path}. "
        f"Commit your project files before running agents. "
        f"Worktree contents:\n{listing or '(empty)'}"
    )


def _recreate_stale_branch(
    svc: GitService,
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    sha: str | None,
) -> None:
    if not svc.is_ancestor(branch, repo_path):
        raise WorktreeError(
            f"Branch {branch!r} has unique commits not yet on the base branch. "
            "Merge or remove these commits before retrying."
        )
    svc.remove_worktree(repo_path, worktree_path)
    with _wrap_git_errors():
        svc.delete_branch(branch, repo_path)
        svc.create_worktree(repo_path, worktree_path, branch, sha)


def create_worktree(
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    sha: str | None = None,
    git_service: GitService | None = None,
    cfg: Config | None = None,
) -> None:
    svc = git_service or GitService(cfg or load_config())
    with _wrap_git_errors():
        branch_exists = svc.verify_ref_exists(branch, repo_path)

        if worktree_path.exists():
            registered = svc.list_worktrees(repo_path)
            if worktree_path in registered:
                if not _has_project_files(worktree_path):
                    error = _missing_files_error(worktree_path)
                    svc.remove_worktree(repo_path, worktree_path)
                    raise error
                return
            svc.remove_worktree(repo_path, worktree_path)

        svc.create_worktree(repo_path, worktree_path, branch, sha)

        if not _has_project_files(worktree_path) and branch_exists:
            _recreate_stale_branch(svc, repo_path, worktree_path, branch, sha)

        if not _has_project_files(worktree_path):
            error = _missing_files_error(worktree_path)
            svc.remove_worktree(repo_path, worktree_path)
            raise error


def remove_worktree(
    repo_path: Path,
    worktree_path: Path,
    git_service: GitService | None = None,
    cfg: Config | None = None,
) -> None:
    svc = git_service or GitService(cfg or load_config())
    try:
        svc.remove_worktree(repo_path, worktree_path)
    except GitTimeoutError as exc:
        raise WorktreeTimeoutError(str(exc)) from exc


@asynccontextmanager
async def managed_worktree(
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    sha: str | None = None,
    git_service: GitService | None = None,
):
    create_worktree(repo_path, worktree_path, branch, sha, git_service)
    try:
        yield worktree_path
    finally:
        remove_worktree(repo_path, worktree_path, git_service)


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
