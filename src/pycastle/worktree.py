import os
import re
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Protocol

from .config import Config
from .errors import WorktreeError, WorktreeTimeoutError
from .services import GitCommandError, GitService, GitTimeoutError

CONTAINER_PARENT_GIT = "/.pycastle-parent-git"


class _WorktreeDeps(Protocol):
    repo_root: Path
    cfg: Config
    git_svc: GitService


def worktree_name_for_branch(branch: str) -> str:
    m = re.match(r"pycastle/issue-(\d+)", branch)
    if m:
        return f"issue-{m.group(1)}"
    return re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")


def worktree_path(name: str, deps: _WorktreeDeps) -> Path:
    return deps.repo_root / deps.cfg.pycastle_dir / ".worktrees" / name


@contextmanager
def _wrap_git_errors():
    try:
        yield
    except GitTimeoutError as exc:
        raise WorktreeTimeoutError(str(exc)) from exc
    except GitCommandError as exc:
        raise WorktreeError(str(exc)) from exc


def _remove_worktrees_dir_if_empty(worktrees_dir: Path) -> None:
    if worktrees_dir.exists() and not any(worktrees_dir.iterdir()):
        worktrees_dir.rmdir()


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


def _create_worktree(
    svc: GitService,
    repo_path: Path,
    worktree_path: Path,
    branch: str,
    sha: str | None = None,
) -> None:
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


@asynccontextmanager
async def branch_worktree(
    name: str,
    branch: str,
    sha: str | None,
    deps: _WorktreeDeps,
    *,
    delete_branch: bool = True,
):
    path = worktree_path(name, deps)
    _create_worktree(deps.git_svc, deps.repo_root, path, branch, sha)
    try:
        yield path
    finally:
        try:
            try:
                deps.git_svc.remove_worktree(deps.repo_root, path)
            finally:
                if delete_branch:
                    deps.git_svc.delete_branch(branch, deps.repo_root)
        finally:
            _remove_worktrees_dir_if_empty(path.parent)


@asynccontextmanager
async def detached_worktree(name: str, sha: str, deps: _WorktreeDeps):
    path = worktree_path(name, deps)
    deps.git_svc.checkout_detached(deps.repo_root, path, sha)
    try:
        yield path
    finally:
        try:
            deps.git_svc.remove_worktree(deps.repo_root, path)
        finally:
            _remove_worktrees_dir_if_empty(path.parent)


def patch_gitdir_for_container(worktree_path: Path) -> Path | None:
    """Return a temp file with the container-internal gitdir path, or None.

    Needed on all platforms: the host parent .git dir is bind-mounted at
    CONTAINER_PARENT_GIT, so the worktree .git file's absolute host path
    cannot be followed inside the container. The host .git file is never
    modified; the caller should bind-mount the returned path over the
    container's .git.
    """
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
