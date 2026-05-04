import shutil
from pathlib import Path

from ..config import Config
from ._base import _SubprocessService


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

    def create_worktree(
        self, repo_path: Path, worktree_path: Path, branch: str, sha: str | None = None
    ) -> None:
        self._run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            capture_output=True,
        )

        if worktree_path.exists():
            self._run_or_raise(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                f"git worktree remove --force {str(worktree_path)!r} failed",
                cwd=repo_path,
            )

        if self.verify_ref_exists(branch, repo_path):
            cmd = ["git", "worktree", "add", str(worktree_path), branch]
        else:
            start_point = sha if sha is not None else "HEAD"
            cmd = [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree_path),
                start_point,
            ]

        self._run_or_raise(cmd, "git worktree add failed", cwd=repo_path)

    def try_merge(self, repo_path: Path, branch: str) -> bool:
        result = self._run(
            ["git", "merge", "--no-edit", branch],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        abort = self._run(
            ["git", "merge", "--abort"], cwd=repo_path, capture_output=True
        )
        if abort.returncode == 0:
            return False
        raise GitCommandError(
            f"git merge --no-edit {branch!r} failed",
            returncode=result.returncode,
            stderr=self._decode(result.stderr),
        )

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
            self._run_or_raise(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                f"git worktree remove --force {str(worktree_path)!r} failed",
                cwd=repo_path,
            )

        self._run_or_raise(
            ["git", "worktree", "add", "--detach", str(worktree_path), sha],
            "git worktree add --detach failed",
            cwd=repo_path,
        )

    def pull(self, repo_path: Path) -> None:
        self._run_or_raise(
            ["git", "pull", "--ff-only"],
            "git pull --ff-only failed",
            cwd=repo_path,
        )

    def commit(self, worktree_path: Path, repo_root: Path, message: str) -> None:
        self._run_or_raise(
            ["git", "-C", str(worktree_path), "add", "-A"],
            "git add -A failed",
            cwd=repo_root,
        )
        self._run_or_raise(
            ["git", "-C", str(worktree_path), "commit", "-m", message],
            "git commit failed",
            cwd=repo_root,
        )

    def push(self, repo_path: Path) -> None:
        self._run_or_raise(
            ["git", "push"],
            "git push failed",
            cwd=repo_path,
        )

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
