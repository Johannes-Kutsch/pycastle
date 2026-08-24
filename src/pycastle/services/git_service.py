import logging
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from pycastle.config import Config
from pycastle.services._git_remote_retry import (
    DEFAULT_REMOTE_GIT_RETRY_POLICY,
    EscalateOperatorActionableGitFailure,
    PassthroughRemoteFailure,
    RecoverPushNonFastForward,
    RemoteGitOperation,
    RemoteGitRetryDecision,
    RetryTransientRemoteFailure,
)
from pycastle.services._operating_branch_refresh import (
    FastForwardLocalRef,
    NoUpstreamYet,
    OperatingBranchRefRelation,
    RefAncestry,
    classify_ref_relation,
)

logger = logging.getLogger(__name__)


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


class OperatingBranchCheckedOutError(GitServiceError):
    """Raised when git refuses to update a ref because the branch is checked out."""

    def __init__(self, branch: str) -> None:
        self.branch = branch
        super().__init__(f"branch {branch!r} is currently checked out")


class GitTimeoutError(GitServiceError, TimeoutError):
    pass


class GitNotFoundError(GitServiceError):
    pass


class GitService:
    def __init__(self, cfg: Config) -> None:
        self.timeout = cfg.worktree_timeout
        self._remote_retry_policy = DEFAULT_REMOTE_GIT_RETRY_POLICY

    def _run(
        self, cmd: list[str], cwd: Path | None = None, **kwargs: object
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        kwargs.setdefault("timeout", self.timeout)
        try:
            return subprocess.run(cmd, cwd=cwd, check=False, **kwargs)  # type: ignore[call-overload]  # noqa: S603  # callers control cmd contents
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(
                f"command timed out after {exc.timeout}s: {exc.cmd}"
            ) from exc
        except FileNotFoundError as exc:
            raise GitNotFoundError(f"executable not found: {cmd[0]}") from exc

    def _run_or_raise(
        self, cmd: list[str], message: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        result = self._run(cmd, cwd=cwd, capture_output=True)
        if result.returncode != 0:
            raise GitCommandError(
                message, result.returncode, self._decode(result.stderr)
            )
        return result

    @staticmethod
    def _decode(b: bytes) -> str:
        return b.decode("utf-8", errors="replace").strip()

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

    def is_ancestor(self, branch: str, repo_path: Path, target: str = "HEAD") -> bool:
        result = self._run(
            ["git", "merge-base", "--is-ancestor", branch, target],
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
        return [path for path, _ in self.list_worktrees_with_branches(repo_path)]

    def list_worktrees_with_branches(
        self, repo_path: Path
    ) -> list[tuple[Path, str | None]]:
        result = self._run_or_raise(
            ["git", "worktree", "list", "--porcelain"],
            "git worktree list failed",
            cwd=repo_path,
        )
        worktrees: list[tuple[Path, str | None]] = []
        current_path: Path | None = None
        current_branch: str | None = None
        for line in self._decode(result.stdout).splitlines():
            if line.startswith("worktree "):
                if current_path is not None:
                    worktrees.append((current_path, current_branch))
                current_path = Path(line[len("worktree ") :])
                current_branch = None
            elif line.startswith("branch "):
                ref = line[len("branch ") :]
                prefix = "refs/heads/"
                current_branch = ref.removeprefix(prefix)
        if current_path is not None:
            worktrees.append((current_path, current_branch))
        return worktrees

    def prune_worktrees(self, repo_path: Path) -> None:
        self._run_or_raise(
            ["git", "worktree", "prune"],
            "git worktree prune failed",
            cwd=repo_path,
        )

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
        if len(parts) != 2 or not all(parts):  # noqa: PLR2004  # owner/repo is exactly 2 path segments
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

    def start_merge(self, repo_path: Path, branch: str) -> bool:
        result = self._run(
            ["git", "merge", "--no-edit", branch],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        stdout = self._decode(result.stdout)
        stderr = self._decode(result.stderr)
        output = "\n".join(part for part in (stdout, stderr) if part)
        if "refusing to merge unrelated histories" in output.lower():
            raise UnrelatedHistoriesError(
                f"git merge --no-edit {branch!r} failed",
                returncode=result.returncode,
                stderr=output,
            )
        if "conflict" in output.lower():
            return False
        raise GitCommandError(
            f"git merge --no-edit {branch!r} failed",
            returncode=result.returncode,
            stderr=output,
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
        return self.count_commits_ahead(repo_path, main_branch) > 0

    def branch_has_commits_ahead_of_merge_base(
        self, repo_path: Path, branch: str, main_branch: str = "main"
    ) -> bool:
        result = self._run(
            ["git", "rev-list", "--count", f"{main_branch}..{branch}"],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            return False
        try:
            return int(self._decode(result.stdout)) > 0
        except ValueError:
            return False

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

    def get_branch_sha(self, repo_path: Path, branch: str) -> str:
        result = self._run_or_raise(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            f"git rev-parse refs/heads/{branch!r} failed",
            cwd=repo_path,
        )
        return self._decode(result.stdout)

    def get_current_branch(self, repo_path: Path) -> str:
        result = self._run_or_raise(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            "git rev-parse --abbrev-ref HEAD failed",
            cwd=repo_path,
        )
        return self._decode(result.stdout)

    def advance_branch_ref(self, repo_path: Path, target: str, source: str) -> None:
        """Advance a local branch ref to source without checking out either branch.

        Uses 'git fetch . source:target' — a local fast-forward ref update that
        succeeds regardless of what is currently checked out in repo_path, unless
        the target branch itself is checked out, in which case raises
        OperatingBranchCheckedOutError.
        """
        result = self._run(
            ["git", "fetch", ".", f"{source}:{target}"],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = self._decode(result.stderr)
            if "refusing to fetch into branch" in stderr.lower():
                raise OperatingBranchCheckedOutError(target)
            raise GitCommandError(
                f"git fetch . {source}:{target} failed",
                result.returncode,
                stderr,
            )

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
        operation: RemoteGitOperation,
        cwd: Path | None = None,
    ) -> None:
        for attempt in range(1, self._remote_retry_policy.max_attempts + 1):
            try:
                self._run_or_raise(cmd, message, cwd=cwd)
            except GitCommandError as exc:
                if self._handle_remote_retry_decision(
                    decision=self._remote_retry_policy.classify_remote_failure(
                        operation, exc.stderr, attempt
                    ),
                    message=message,
                    operation=operation,
                    attempt=attempt,
                    stderr=exc.stderr,
                    cause=exc,
                ):
                    continue
            else:
                if attempt > 1:
                    logger.warning(
                        "git %s succeeded on attempt %d after transient failure",
                        operation,
                        attempt,
                    )
                return

    def _handle_remote_retry_decision(
        self,
        *,
        decision: RemoteGitRetryDecision,
        message: str,
        operation: str,
        attempt: int,
        stderr: str,
        cause: GitCommandError,
    ) -> bool:
        if isinstance(decision, EscalateOperatorActionableGitFailure):
            raise OperatorActionableGitError(
                message,
                stderr=stderr,
                op=operation,
                attempt_count=attempt,
            ) from cause
        if isinstance(decision, PassthroughRemoteFailure):
            raise cause
        if isinstance(decision, RetryTransientRemoteFailure):
            logger.warning(
                "git %s failed (attempt %d/%d), retrying in %ds: %s",
                operation,
                attempt,
                self._remote_retry_policy.max_attempts,
                decision.delay_seconds,
                stderr,
            )
            time.sleep(decision.delay_seconds)
            return True
        return False

    def pull(self, repo_path: Path) -> None:
        self._run_or_raise_with_retry(
            ["git", "pull", "--ff-only"],
            "git pull --ff-only failed",
            operation="pull",
            cwd=repo_path,
        )

    def pull_with_merge_fallback(
        self, repo_path: Path, *, branch: str | None = None
    ) -> None:
        try:
            self._run_or_raise_with_retry(
                ["git", "pull", "--ff-only"],
                "git pull --ff-only failed",
                operation="pull",
                cwd=repo_path,
            )
        except GitCommandError as exc:
            if "not possible to fast-forward" not in exc.stderr.lower():
                raise
        else:
            return
        effective_branch = (
            branch if branch is not None else self.get_current_branch(repo_path)
        )
        merged = self.try_merge(repo_path, f"origin/{effective_branch}")
        if not merged:
            raise GitCommandError(
                f"git merge origin/{effective_branch} failed due to conflicts",
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

    def fetch_branch(self, repo_path: Path, branch: str) -> None:
        """Fetch and fast-forward a local branch ref from origin without checkout.

        Raises GitCommandError when the fetch cannot fast-forward (diverged histories).
        Retries transient remote failures per the operator-actionable retry profile.
        """
        self._run_or_raise_with_retry(
            ["git", "fetch", "origin", f"{branch}:{branch}"],
            f"git fetch origin {branch}:{branch} failed",
            operation="fetch",
            cwd=repo_path,
        )

    def refresh_operating_branch(
        self, repo_path: Path, branch: str
    ) -> OperatingBranchRefRelation:
        """Two-step operating-branch refresh per ADR 0062.

        Step 1: forced fetch updates refs/remotes/origin/<branch> (cannot be rejected).
        Step 2: classify the ref relation and fast-forward the local ref if appropriate.
        Returns the OperatingBranchRefRelation outcome.
        """
        refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        try:
            self._run_or_raise_with_retry(
                ["git", "fetch", "origin", refspec],
                f"git fetch origin {refspec} failed",
                operation="fetch",
                cwd=repo_path,
            )
        except OperatorActionableGitError as exc:
            if "couldn't find remote ref" in exc.stderr.lower():
                return NoUpstreamYet()
            raise

        local_ref = f"refs/heads/{branch}"
        remote_ref = f"refs/remotes/origin/{branch}"

        local_sha = self._decode(
            self._run(
                ["git", "rev-parse", local_ref], cwd=repo_path, capture_output=True
            ).stdout
        )
        remote_sha = self._decode(
            self._run(
                ["git", "rev-parse", remote_ref], cwd=repo_path, capture_output=True
            ).stdout
        )

        ancestry: RefAncestry
        if local_sha == remote_sha:
            ancestry = "equal"
        else:
            is_remote_ancestor = (
                self._run(
                    ["git", "merge-base", "--is-ancestor", remote_ref, local_ref],
                    cwd=repo_path,
                    capture_output=True,
                ).returncode
                == 0
            )
            if is_remote_ancestor:
                ancestry = "local_ahead"
            else:
                is_local_ancestor = (
                    self._run(
                        ["git", "merge-base", "--is-ancestor", local_ref, remote_ref],
                        cwd=repo_path,
                        capture_output=True,
                    ).returncode
                    == 0
                )
                ancestry = "remote_ahead" if is_local_ancestor else "diverged"

        relation: OperatingBranchRefRelation = classify_ref_relation(
            upstream_ref_exists=True, ancestry=ancestry
        )

        if isinstance(relation, FastForwardLocalRef):
            self.advance_branch_ref(repo_path, branch, f"origin/{branch}")

        return relation

    async def push(
        self,
        repo_path: Path,
        branch: str,
        resolver: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        for attempt in range(1, self._remote_retry_policy.max_attempts + 1):
            try:
                self._run_or_raise(
                    ["git", "push", "origin", branch],
                    "git push failed",
                    cwd=repo_path,
                )
            except GitCommandError as exc:
                decision = self._remote_retry_policy.classify_remote_failure(
                    "push", exc.stderr, attempt
                )
                if isinstance(decision, RecoverPushNonFastForward):
                    if attempt == self._remote_retry_policy.max_attempts:
                        raise
                    logger.warning(
                        "git push rejected non-fast-forward (attempt %d/%d), pulling with merge fallback",
                        attempt,
                        self._remote_retry_policy.max_attempts,
                    )
                    try:
                        self.pull_with_merge_fallback(repo_path, branch=branch)
                    except GitCommandError as pull_err:
                        if resolver is None or "conflict" not in str(pull_err).lower():
                            raise
                        await resolver()
                    continue
                if self._handle_remote_retry_decision(
                    decision=decision,
                    message="git push failed",
                    operation="push",
                    attempt=attempt,
                    stderr=exc.stderr,
                    cause=exc,
                ):
                    continue
            else:
                if attempt > 1:
                    logger.warning(
                        "git push succeeded on attempt %d after transient failure",
                        attempt,
                    )
                return

    def checkout_branch(self, repo_path: Path, branch: str) -> None:
        self._run_or_raise(
            ["git", "checkout", branch],
            f"git checkout {branch!r} failed",
            cwd=repo_path,
        )

    def create_branch_from(self, repo_path: Path, branch: str, source: str) -> None:
        self._run_or_raise(
            ["git", "branch", branch, source],
            f"git branch {branch!r} {source!r} failed",
            cwd=repo_path,
        )

    def push_upstream(self, repo_path: Path, branch: str) -> None:
        self._run_or_raise_with_retry(
            ["git", "push", "-u", "origin", branch],
            f"git push -u origin {branch!r} failed",
            operation="push",
            cwd=repo_path,
        )

    def remove_worktree(self, repo_path: Path, worktree_path: Path) -> None:
        result = self._run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            shutil.rmtree(worktree_path, ignore_errors=True)
