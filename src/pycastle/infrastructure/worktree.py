import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from contextlib import asynccontextmanager, contextmanager, suppress
from enum import Enum
from pathlib import Path
from typing import Protocol

from ..config import Config, load_config
from ..errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
    WorktreeError,
    WorktreeTimeoutError,
)
from ..services import GitCommandError, GitService, GitTimeoutError
from ..session import SESSION_DIR_NAME, any_role_dir_present

CONTAINER_PARENT_GIT = "/.pycastle-parent-git"
PRESERVED_FAILURE_MARKER = ".preserved-failure"
PROJECT_LOCAL_PYCASTLE_DIR = Path("pycastle")


class _WorktreeDeps(Protocol):
    repo_root: Path
    git_svc: GitService


@dataclass(frozen=True)
class WorktreeIdentity:
    branch: str
    name: str
    path: Path


class DurableIssueWorktreeIntent(str, Enum):
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"


class ReusableSandboxWorktreeIntent(str, Enum):
    IMPROVE = "improve-sandbox"
    DIVERGENCE = "diverge-sandbox"


class _SandboxLifecycle(str, Enum):
    REUSABLE = "reusable"
    REPLACEABLE_MERGE = "replaceable-merge"


class DetachedTransientWorktreeIntent(str, Enum):
    PLAN = "plan-sandbox"
    PREFLIGHT = "preflight-sandbox"


def _worktree_name_for_branch(branch: str) -> str:
    m = re.match(r"pycastle/issue-(\d+)", branch)
    if m:
        return f"issue-{m.group(1)}"
    m = re.match(r"pycastle/([a-z0-9-]*sandbox-issue-\d+)$", branch)
    if m:
        return m.group(1)
    m = re.match(r"pycastle/([a-z0-9-]*sandbox)$", branch)
    if m:
        return m.group(1)
    return re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")


def _project_local_worktrees_dir(repo_root: Path) -> Path:
    return repo_root / PROJECT_LOCAL_PYCASTLE_DIR / ".worktrees"


def worktree_identity(
    branch: str, repo_root: Path, *, name: str | None = None
) -> WorktreeIdentity:
    resolved_name = name or _worktree_name_for_branch(branch)
    return WorktreeIdentity(
        branch=branch,
        name=resolved_name,
        path=_project_local_worktrees_dir(repo_root) / resolved_name,
    )


def issue_branch(issue_number: int) -> str:
    return f"pycastle/issue-{issue_number}"


def _reusable_sandbox_intent_name(intent: ReusableSandboxWorktreeIntent | str) -> str:
    return intent.value if isinstance(intent, ReusableSandboxWorktreeIntent) else intent


def _detached_transient_intent_name(
    intent: DetachedTransientWorktreeIntent | str,
) -> str:
    return (
        intent.value if isinstance(intent, DetachedTransientWorktreeIntent) else intent
    )


def reusable_sandbox_worktree_identity(
    intent: ReusableSandboxWorktreeIntent | str, repo_root: Path
) -> WorktreeIdentity:
    intent_name = _reusable_sandbox_intent_name(intent)
    return worktree_identity(f"pycastle/{intent_name}", repo_root, name=intent_name)


def merge_sandbox_worktree_identity(
    issue_number: int, repo_root: Path
) -> WorktreeIdentity:
    return worktree_identity(
        f"pycastle/merge-sandbox-issue-{issue_number}",
        repo_root,
    )


def detached_transient_worktree_path(
    intent: DetachedTransientWorktreeIntent | str,
    repo_root: Path,
) -> Path:
    return worktree_path(_detached_transient_intent_name(intent), repo_root)


def worktree_name_for_branch(branch: str) -> str:
    return _worktree_name_for_branch(branch)


def worktree_path(name: str, repo_root: Path) -> Path:
    return _project_local_worktrees_dir(repo_root) / name


@contextmanager
def _wrap_git_errors():
    try:
        yield
    except GitTimeoutError as exc:
        raise WorktreeTimeoutError(str(exc)) from exc
    except GitCommandError as exc:
        raise WorktreeError(str(exc)) from exc


def remove_worktrees_dir_if_empty(worktrees_dir: Path) -> None:
    if worktrees_dir.exists() and not any(worktrees_dir.iterdir()):
        worktrees_dir.rmdir()


def is_failure_worktree_preserved(path: Path) -> bool:
    marker = path / SESSION_DIR_NAME / PRESERVED_FAILURE_MARKER
    return marker.is_file()


def mark_failure_worktree_preserved(path: Path) -> None:
    marker = path / SESSION_DIR_NAME / PRESERVED_FAILURE_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")


def prune_orphan_worktrees(
    repo_root: Path,
    cfg: Config | None = None,
    git_service: GitService | None = None,
) -> None:
    if git_service is not None:
        svc = git_service
    else:
        resolved_cfg = cfg or load_config()
        svc = GitService(resolved_cfg)
    worktrees_dir = _project_local_worktrees_dir(repo_root)
    if not worktrees_dir.exists():
        return
    active = {str(p) for p in svc.list_worktrees(repo_root)}
    for child in list(worktrees_dir.iterdir()):
        if not child.is_dir():
            continue
        if is_failure_worktree_preserved(child):
            continue
        if str(child.resolve()) not in active:
            shutil.rmtree(child)
    remove_worktrees_dir_if_empty(worktrees_dir)


def teardown_worktree(svc: GitService, repo_root: Path, path: Path) -> None:
    try:
        svc.remove_worktree(repo_root, path)
    finally:
        remove_worktrees_dir_if_empty(path.parent)


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


def is_worktree_reusable(path: Path, branch: str, git_svc: GitService) -> bool:
    if not path.exists():
        return False
    try:
        current = git_svc.get_current_branch(path)
    except Exception:
        return False
    return current == branch and any_role_dir_present(path)


def _cleanup_stale_sandbox(
    svc: GitService,
    repo_path: Path,
    wt_path: Path,
    branch: str,
    *,
    lifecycle: _SandboxLifecycle,
) -> None:
    """Remove any stale worktree and/or branch for an ephemeral sandbox.

    Called before creating an ephemeral sandbox so the new worktree is built
    fresh at the caller-supplied SHA, regardless of what a prior run left
    behind. Best-effort: subsequent _create_worktree surfaces real errors.
    """
    if (
        lifecycle is not _SandboxLifecycle.REPLACEABLE_MERGE
        and is_failure_worktree_preserved(wt_path)
    ):
        return
    try:
        registered = svc.list_worktrees(repo_path)
    except Exception:
        registered = []
    if wt_path.exists() or wt_path in registered:
        with suppress(Exception):
            svc.remove_worktree(repo_path, wt_path)
    with suppress(Exception):
        if svc.verify_ref_exists(branch, repo_path):
            svc.delete_branch(branch, repo_path)


@asynccontextmanager
async def managed_worktree(
    name: str | None = None,
    *,
    branch: str | None = None,
    identity: WorktreeIdentity | None = None,
    sha: str | None,
    delete_branch_on_teardown: bool,
    sandbox_lifecycle: _SandboxLifecycle = _SandboxLifecycle.REUSABLE,
    deps: _WorktreeDeps,
):
    resolved_identity = identity
    if resolved_identity is None:
        if name is None or branch is None:
            raise TypeError(
                "managed_worktree requires either identity or both name and branch"
            )
        resolved_identity = worktree_identity(branch, deps.repo_root, name=name)
    path = resolved_identity.path
    if delete_branch_on_teardown:
        _cleanup_stale_sandbox(
            deps.git_svc,
            deps.repo_root,
            path,
            resolved_identity.branch,
            lifecycle=sandbox_lifecycle,
        )
        _create_worktree(
            deps.git_svc, deps.repo_root, path, resolved_identity.branch, sha
        )
    elif not is_worktree_reusable(path, resolved_identity.branch, deps.git_svc):
        _create_worktree(
            deps.git_svc, deps.repo_root, path, resolved_identity.branch, sha
        )
    _preservation_worthy_exc = False
    try:
        yield path
    except (UsageLimitError, TransientAgentError):
        raise
    except AgentCredentialFailureError:
        raise
    except (AgentFailedError, HardAgentError):
        _preservation_worthy_exc = True
        mark_failure_worktree_preserved(path)
        raise
    except Exception:
        _preservation_worthy_exc = True
        mark_failure_worktree_preserved(path)
        raise
    finally:
        try:
            dirty = not deps.git_svc.is_working_tree_clean(path)
        except Exception:
            dirty = True
        if not (_preservation_worthy_exc or dirty or any_role_dir_present(path)):
            try:
                _branch_has_commits = deps.git_svc.has_commits_ahead_of_main(path)
            except Exception:
                _branch_has_commits = True
            teardown_worktree(deps.git_svc, deps.repo_root, path)
            if delete_branch_on_teardown or not _branch_has_commits:
                deps.git_svc.delete_branch(resolved_identity.branch, deps.repo_root)


@asynccontextmanager
async def durable_issue_worktree(
    issue_number: int,
    *,
    intent: DurableIssueWorktreeIntent,
    deps: _WorktreeDeps,
    planner_sha: str | None = None,
):
    identity = worktree_identity(issue_branch(issue_number), deps.repo_root)
    sha = planner_sha if intent is DurableIssueWorktreeIntent.IMPLEMENTER else None
    async with managed_worktree(
        identity=identity,
        sha=sha,
        delete_branch_on_teardown=False,
        deps=deps,
    ) as path:
        yield path


@asynccontextmanager
async def reusable_sandbox_worktree(
    intent: ReusableSandboxWorktreeIntent | str,
    *,
    sha: str | None,
    deps: _WorktreeDeps,
):
    identity = reusable_sandbox_worktree_identity(intent, deps.repo_root)
    async with managed_worktree(
        identity=identity,
        sha=sha,
        delete_branch_on_teardown=True,
        deps=deps,
    ) as path:
        yield path


@asynccontextmanager
async def replaceable_merge_sandbox_worktree(
    issue_number: int,
    *,
    sha: str | None,
    deps: _WorktreeDeps,
):
    identity = merge_sandbox_worktree_identity(issue_number, deps.repo_root)
    async with managed_worktree(
        identity=identity,
        sha=sha,
        delete_branch_on_teardown=True,
        sandbox_lifecycle=_SandboxLifecycle.REPLACEABLE_MERGE,
        deps=deps,
    ) as path:
        yield path


@asynccontextmanager
async def _detached_checkout_lifecycle(
    path: Path, *, sha: str | None, deps: _WorktreeDeps
):
    if sha is not None:
        deps.git_svc.checkout_detached(deps.repo_root, path, sha)
    _preserve = False
    try:
        yield path
    except AgentFailedError:
        _preserve = True
        mark_failure_worktree_preserved(path)
        raise
    finally:
        if not _preserve:
            teardown_worktree(deps.git_svc, deps.repo_root, path)


@asynccontextmanager
async def transient_worktree(name: str, *, sha: str | None, deps: _WorktreeDeps):
    async with _detached_checkout_lifecycle(
        worktree_path(name, deps.repo_root),
        sha=sha,
        deps=deps,
    ) as path:
        yield path


@asynccontextmanager
async def detached_transient_worktree(
    intent: DetachedTransientWorktreeIntent | str,
    *,
    sha: str | None,
    deps: _WorktreeDeps,
):
    async with _detached_checkout_lifecycle(
        detached_transient_worktree_path(intent, deps.repo_root),
        sha=sha,
        deps=deps,
    ) as path:
        yield path


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
