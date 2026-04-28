import shutil
import subprocess
from pathlib import Path

from .config import WORKTREE_TIMEOUT


class GitServiceError(RuntimeError):
    pass


class GitCommandError(GitServiceError):
    def __init__(self, message: str, returncode: int = -1, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)


class GitTimeoutError(GitServiceError, TimeoutError):
    pass


class GitNotFoundError(GitServiceError):
    pass


class GitService:
    def __init__(self, timeout: int = WORKTREE_TIMEOUT) -> None:
        self.timeout = timeout

    def _run(
        self, cmd: list[str], cwd: Path | None = None, **kwargs: object
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        kwargs.setdefault("timeout", self.timeout)
        try:
            return subprocess.run(cmd, cwd=cwd, **kwargs)  # type: ignore[call-overload]
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(
                f"git command timed out after {self.timeout}s: {exc.cmd}"
            ) from exc
        except FileNotFoundError as exc:
            raise GitNotFoundError("git executable not found") from exc

    def get_user_name(self, cwd: Path | None = None) -> str:
        result = self._run(["git", "config", "user.name"], cwd=cwd, capture_output=True)
        if result.returncode != 0:
            raise GitCommandError(
                "git config user.name failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def get_user_email(self, cwd: Path | None = None) -> str:
        result = self._run(
            ["git", "config", "user.email"], cwd=cwd, capture_output=True
        )
        if result.returncode != 0:
            raise GitCommandError(
                "git config user.email failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

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
        result = self._run(
            ["git", "branch", "-D", branch],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            raise GitCommandError(
                f"git branch -D {branch!r} failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )

    def list_worktrees(self, repo_path: Path) -> list[Path]:
        result = self._run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            raise GitCommandError(
                "git worktree list failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        paths: list[Path] = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree ") :]))
        return paths

    def get_remote_url(self, remote: str = "origin", cwd: Path | None = None) -> str:
        result = self._run(
            ["git", "remote", "get-url", remote],
            cwd=cwd,
            capture_output=True,
        )
        if result.returncode != 0:
            raise GitCommandError(
                f"git remote get-url {remote!r} failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def create_worktree(
        self, repo_path: Path, worktree_path: Path, branch: str
    ) -> None:
        self._run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            capture_output=True,
        )

        if worktree_path.exists():
            self._run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=repo_path,
                capture_output=True,
            )

        if self.verify_ref_exists(branch, repo_path):
            cmd = ["git", "worktree", "add", str(worktree_path), branch]
        else:
            cmd = ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"]

        result = self._run(cmd, cwd=repo_path, capture_output=True)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitCommandError(
                f"git worktree add failed: {stderr}",
                returncode=result.returncode,
                stderr=stderr,
            )

    def remove_worktree(self, repo_path: Path, worktree_path: Path) -> None:
        result = self._run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode != 0:
            shutil.rmtree(worktree_path, ignore_errors=True)
