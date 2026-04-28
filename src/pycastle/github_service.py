import json
import subprocess

from .config import WORKTREE_TIMEOUT


class GithubServiceError(RuntimeError):
    pass


class GithubCommandError(GithubServiceError):
    def __init__(self, message: str, returncode: int = -1, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(message)


class GithubTimeoutError(GithubServiceError, TimeoutError):
    pass


class GithubNotFoundError(GithubServiceError):
    pass


class GithubService:
    def __init__(self, repo: str, timeout: int = WORKTREE_TIMEOUT) -> None:
        self.repo = repo
        self.timeout = timeout

    def _run(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        kwargs.setdefault("timeout", self.timeout)
        try:
            return subprocess.run(cmd, **kwargs)  # type: ignore[call-overload]
        except subprocess.TimeoutExpired as exc:
            raise GithubTimeoutError(
                f"gh command timed out after {self.timeout}s: {exc.cmd}"
            ) from exc
        except FileNotFoundError as exc:
            raise GithubNotFoundError("gh executable not found") from exc

    def close_issue(self, number: int) -> None:
        result = self._run(["gh", "issue", "close", str(number)], capture_output=True)
        if result.returncode != 0:
            raise GithubCommandError(
                f"gh issue close {number} failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )

    def get_parent(self, number: int) -> int | None:
        result = self._run(
            [
                "gh",
                "api",
                f"repos/{self.repo}/issues/{number}",
                "--jq",
                ".parent.number",
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            raise GithubCommandError(
                f"gh api repos/{self.repo}/issues/{number} failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        output = result.stdout.decode("utf-8", errors="replace").strip()
        if not output or output == "null":
            return None
        return int(output)

    def get_open_sub_issues(self, number: int) -> list[int]:
        result = self._run(
            ["gh", "api", f"repos/{self.repo}/issues/{number}/sub_issues"],
            capture_output=True,
        )
        if result.returncode != 0:
            raise GithubCommandError(
                f"gh api repos/{self.repo}/issues/{number}/sub_issues failed",
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        data: list[dict[str, object]] = json.loads(
            result.stdout.decode("utf-8", errors="replace")
        )
        return [
            int(str(item["number"])) for item in data if item.get("state") == "open"
        ]

    def close_issue_with_parents(self, number: int) -> None:
        self.close_issue(number)
        parent = self.get_parent(number)
        if parent is None:
            return
        open_siblings = self.get_open_sub_issues(parent)
        if open_siblings:
            return
        self.close_issue_with_parents(parent)
