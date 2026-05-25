import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..config import Config
from ._base import _SubprocessService

logger = logging.getLogger(__name__)

_PERMANENT_FAILURE_PATTERNS = [
    "not possible to fast-forward",
    "need to specify how to reconcile divergent branches",
    "refusing to merge unrelated histories",
    "conflict",
]

_OPERATOR_ACTIONABLE_PATTERNS = [
    "repository not found",
    "remote: not found",
    "does not appear to be a git repository",
]

_NFF_PUSH_PATTERNS = ["[rejected]"]

_RETRY_DELAYS = [10, 60, 300]
_MAX_ATTEMPTS = 4


class GitServiceError(RuntimeError):
    pass


class GitCommandError(GitServiceError):
    def __init__(self, message: str, returncode: int = -1, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)

    def __str__(self) -> str:
        base = super().__str__()
        parts = [base, f"returncode: {self.returncode}"]
        if self.stderr:
            parts.append(f"stderr: {self.stderr}")
        return "\n".join(parts)


class OperatorActionableGitError(GitServiceError):
    """Raised when a remote git op fails due to operator-actionable conditions.

    Covers retry exhaustion on transient failures and immediate stable
    misconfigs (repository not found, does not appear to be a git repository).
    Mutually exclusive with the divergence/conflict path.
    """

    def __init__(self, message: str, stderr: str, op: str, attempt_count: int) -> None:
        self.stderr = stderr
        self.op = op
        self.attempt_count = attempt_count
        super().__init__(message)


class UnrelatedHistoriesError(GitCommandError):
    pass


class GitTimeoutError(GitServiceError, TimeoutError):
    pass


class GitNotFoundError(GitServiceError):
    pass


class GitService(_SubprocessService):
    _timeout_error_class = GitTimeoutError
    _not_found_error_class = GitNotFoundError
    _command_error_class = GitCommandError

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg.worktree_timeout)

    def get_user_name(self, cwd: Path | None = None) -> str:
        result = self._run_or_raise(
            ["git", "config", "user.name"], "git config user.name failed", cwd=cwd
        )
        return self._decode(result.stdout)

    def get_user_email(self, cwd: Path | None = None) -> str:
        result = self._run_or_raise(
            ["git", "config", "user.email"], "git config user.email failed", cwd=cwd
        )
        return self._decode(result.stdout)

    def is_ancestor(self, branch: str, repo_path: Path) -> bool:
        result = self._run(
            ["git", "merge-base", "--is-ancestor", branch, "HEAD"],
            cwd=repo_path,
            capture_output=True,
        )
        return result.returncode == 0

    def verify_ref_exists(self, ref: str, repo_path: Path) -> bool:
        result = self._run(
            ["git", "rev-parse", "--verify", ref],
            cwd=repo_path,
            capture_output=True,
        )
        return result.returncode == 0

    def delete_branch(self, branch: str, repo_path: Path) -> None:
        self._run_or_raise(
            ["git", "branch", "-D", branch],
            f"git branch -D {branch!r} failed",
            cwd=repo_path,
        )

    def list_worktrees(self, repo_path: Path) -> list[Path]:
        result = self._run_or_raise(
            ["git", "worktree", "list", "--porcelain"],
            "git worktree list failed",
            cwd=repo_path,
        )
        paths: list[Path] = []
        for line in self._decode(result.stdout).splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree ") :]))
        return paths

    def get_remote_url(self, remote: str = "origin", cwd: Path | None = None) -> str:
        result = self._run_or_raise(
            ["git", "remote", "get-url", remote],
            f"git remote get-url {remote!r} failed",
            cwd=cwd,
        )
        return self._decode(result.stdout)

    def get_github_remote_repo(self, cwd: Path | None = None) -> tuple[str, str] | None:
        try:
            url = self.get_remote_url("origin", cwd=cwd)
        except GitServiceError:
            return None
        for separator in ("github.com/", "github.com:"):
            if separator in url:
                path = url.split(separator, 1)[1]
                break
        else:
            return None
        path = path.removesuffix(".git").strip("/")
        parts = path.split("/")
        if len(parts) != 2 or not all(parts):
            return None
        owner, repo = parts
        return owner, repo

    def _normalize_line_endings(self, worktree_path: Path) -> None:
        self._run_or_raise(
            [
                "git",
                "-C",
                str(worktree_path),
                "-c",
                "core.autocrlf=false",
                "checkout-index",
                "--force",
                "--all",
            ],
            "git checkout-index failed",
        )

    def create_worktree(
        self, repo_path: Path, worktree_path: Path, branch: str, sha: str | None = None
    ) -> None:
        self._run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            capture_output=True,
        )

        if worktree_path.exists():
            self.remove_worktree(repo_path, worktree_path)

        if self.verify_ref_exists(branch, repo_path):
            cmd = [
                "git",
                "-c",
                "core.autocrlf=false",
                "worktree",
                "add",
                str(worktree_path),
                branch,
            ]
        else:
            start_point = sha if sha is not None else "HEAD"
            cmd = [
                "git",
                "-c",
                "core.autocrlf=false",
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree_path),
                start_point,
            ]

        self._run_or_raise(cmd, "git worktree add failed", cwd=repo_path)
        self._normalize_line_endings(worktree_path)

    def try_merge(self, repo_path: Path, branch: str) -> bool:
        result = self._run(
            ["git", "merge", "--no-edit", branch],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        stderr = self._decode(result.stderr)
        if "refusing to merge unrelated histories" in stderr.lower():
            raise UnrelatedHistoriesError(
                f"git merge --no-edit {branch!r} failed",
                returncode=result.returncode,
                stderr=stderr,
            )
        abort = self._run(
            ["git", "merge", "--abort"], cwd=repo_path, capture_output=True
        )
        if abort.returncode == 0:
            return False
        raise GitCommandError(
            f"git merge --no-edit {branch!r} failed",
            returncode=result.returncode,
            stderr=stderr,
        )

    def count_commits_ahead(self, repo_path: Path, remote_ref: str) -> int:
        result = self._run_or_raise(
            ["git", "rev-list", "--count", f"{remote_ref}..HEAD"],
            f"git rev-list --count {remote_ref}..HEAD failed",
            cwd=repo_path,
        )
        return int(self._decode(result.stdout))

    def has_commits_ahead_of_main(
        self, repo_path: Path, main_branch: str = "main"
    ) -> bool:
        merge_base = self._run_or_raise(
            ["git", "merge-base", "HEAD", main_branch],
            f"git merge-base HEAD {main_branch!r} failed",
            cwd=repo_path,
        )
        base_sha = self._decode(merge_base.stdout).strip()
        result = self._run_or_raise(
            ["git", "rev-list", "--count", f"{base_sha}..HEAD"],
            f"git rev-list --count {base_sha}..HEAD failed",
            cwd=repo_path,
        )
        return int(self._decode(result.stdout)) > 0

    def hard_reset_to(self, repo_path: Path, ref: str) -> None:
        self._run_or_raise(
            ["git", "reset", "--hard", ref],
            f"git reset --hard {ref!r} failed",
            cwd=repo_path,
        )

    def get_local_only_commit_subjects(
        self, repo_path: Path, remote_ref: str
    ) -> list[str]:
        result = self._run(
            ["git", "log", f"{remote_ref}..HEAD", "--format=%s"],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            return []
        return [line for line in self._decode(result.stdout).splitlines() if line]

    def is_working_tree_clean(self, repo_path: Path) -> bool:
        result = self._run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
        )
        lines = self._decode(result.stdout).splitlines()
        return all(line.startswith("??") for line in lines)

    def get_head_sha(self, repo_path: Path) -> str:
        result = self._run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
        )
        return self._decode(result.stdout)

    def get_current_branch(self, repo_path: Path) -> str:
        result = self._run_or_raise(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            "git rev-parse --abbrev-ref HEAD failed",
            cwd=repo_path,
        )
        return self._decode(result.stdout)

    def fast_forward_branch(self, repo_path: Path, target: str, source: str) -> None:
        self._run_or_raise(
            ["git", "checkout", target],
            f"git checkout {target!r} failed",
            cwd=repo_path,
        )
        self._run_or_raise(
            ["git", "merge", "--ff-only", source],
            f"git merge --ff-only {source!r} failed",
            cwd=repo_path,
        )

    def checkout_detached(self, repo_path: Path, worktree_path: Path, sha: str) -> None:
        self._run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            capture_output=True,
        )

        if worktree_path.exists():
            self.remove_worktree(repo_path, worktree_path)

        self._run_or_raise(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "worktree",
                "add",
                "--detach",
                str(worktree_path),
                sha,
            ],
            "git worktree add --detach failed",
            cwd=repo_path,
        )
        self._normalize_line_endings(worktree_path)

    def _run_or_raise_with_retry(
        self,
        cmd: list[str],
        message: str,
        operation: str,
        cwd: Path | None = None,
    ) -> None:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                self._run_or_raise(cmd, message, cwd=cwd)
            except GitCommandError as exc:
                stderr_lower = exc.stderr.lower()
                if any(p in stderr_lower for p in _OPERATOR_ACTIONABLE_PATTERNS):
                    raise OperatorActionableGitError(
                        message,
                        stderr=exc.stderr,
                        op=operation,
                        attempt_count=attempt,
                    ) from exc
                if any(p in stderr_lower for p in _PERMANENT_FAILURE_PATTERNS):
                    raise
                if attempt == _MAX_ATTEMPTS:
                    raise OperatorActionableGitError(
                        message,
                        stderr=exc.stderr,
                        op=operation,
                        attempt_count=attempt,
                    ) from exc
                delay = _RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "git %s failed (attempt %d/%d), retrying in %ds: %s",
                    operation,
                    attempt,
                    _MAX_ATTEMPTS,
                    delay,
                    exc.stderr,
                )
                time.sleep(delay)
            else:
                if attempt > 1:
                    logger.warning(
                        "git %s succeeded on attempt %d after transient failure",
                        operation,
                        attempt,
                    )
                return

    def pull(self, repo_path: Path) -> None:
        self._run_or_raise_with_retry(
            ["git", "pull", "--ff-only"],
            "git pull --ff-only failed",
            operation="pull",
            cwd=repo_path,
        )

    def pull_with_merge_fallback(self, repo_path: Path) -> None:
        try:
            self._run_or_raise_with_retry(
                ["git", "pull", "--ff-only"],
                "git pull --ff-only failed",
                operation="pull",
                cwd=repo_path,
            )
            return
        except GitCommandError as exc:
            if "not possible to fast-forward" not in exc.stderr.lower():
                raise
        branch = self.get_current_branch(repo_path)
        merged = self.try_merge(repo_path, f"origin/{branch}")
        if not merged:
            raise GitCommandError(
                f"git merge origin/{branch} failed due to conflicts",
                returncode=1,
                stderr="",
            )

    def commit(self, worktree_path: Path, repo_root: Path, message: str) -> bool:
        self._run_or_raise(
            ["git", "-C", str(worktree_path), "add", "-A"],
            "git add -A failed",
            cwd=repo_root,
        )
        diff_result = self._run(
            ["git", "-C", str(worktree_path), "diff", "--cached", "--quiet"],
            cwd=repo_root,
            capture_output=True,
        )
        if diff_result.returncode == 0:
            return False
        self._run_or_raise(
            ["git", "-C", str(worktree_path), "commit", "-m", message],
            "git commit failed",
            cwd=repo_root,
        )
        return True

    def fetch(self, repo_path: Path) -> None:
        self._run_or_raise_with_retry(
            ["git", "fetch"],
            "git fetch failed",
            operation="fetch",
            cwd=repo_path,
        )

    async def push(
        self,
        repo_path: Path,
        resolver: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            result = self._run(["git", "push"], cwd=repo_path, capture_output=True)
            if result.returncode == 0:
                if attempt > 1:
                    logger.warning(
                        "git push succeeded on attempt %d after transient failure",
                        attempt,
                    )
                return

            stderr = self._decode(result.stderr)
            stderr_lower = stderr.lower()
            is_last = attempt == _MAX_ATTEMPTS

            if any(p in stderr_lower for p in _OPERATOR_ACTIONABLE_PATTERNS):
                raise OperatorActionableGitError(
                    "git push failed",
                    stderr=stderr,
                    op="push",
                    attempt_count=attempt,
                )

            if any(p in stderr_lower for p in _PERMANENT_FAILURE_PATTERNS):
                raise GitCommandError("git push failed", result.returncode, stderr)

            if any(p in stderr for p in _NFF_PUSH_PATTERNS):
                if is_last:
                    raise GitCommandError("git push failed", result.returncode, stderr)
                logger.warning(
                    "git push rejected non-fast-forward (attempt %d/%d), pulling with merge fallback",
                    attempt,
                    _MAX_ATTEMPTS,
                )
                try:
                    self.pull_with_merge_fallback(repo_path)
                except GitCommandError as pull_err:
                    if resolver is None or "conflict" not in str(pull_err).lower():
                        raise
                    await resolver()
                continue

            if is_last:
                raise OperatorActionableGitError(
                    "git push failed",
                    stderr=stderr,
                    op="push",
                    attempt_count=attempt,
                )

            delay = _RETRY_DELAYS[attempt - 1]
            logger.warning(
                "git push failed (attempt %d/%d), retrying in %ds: %s",
                attempt,
                _MAX_ATTEMPTS,
                delay,
                stderr,
            )
            time.sleep(delay)

    def get_branch_commit_subjects(self, branch: str, repo_path: Path) -> list[str]:
        result = self._run(
            ["git", "log", f"main..{branch}", "--format=%s"],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            return []
        output = self._decode(result.stdout)
        return [line for line in output.splitlines() if line]

    def remove_worktree(self, repo_path: Path, worktree_path: Path) -> None:
        result = self._run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            shutil.rmtree(worktree_path, ignore_errors=True)
