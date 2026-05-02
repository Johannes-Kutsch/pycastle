import json

from ..config import Config
from ._base import _SubprocessService


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


class GithubService(_SubprocessService):
    _timeout_error_class = GithubTimeoutError
    _not_found_error_class = GithubNotFoundError
    _command_error_class = GithubCommandError

    def __init__(self, repo: str, cfg: Config) -> None:
        self.repo = repo
        super().__init__(cfg.worktree_timeout)

    def close_issue(self, number: int) -> None:
        self._run_or_raise(
            ["gh", "issue", "close", str(number)],
            f"gh issue close {number} failed",
        )

    def get_parent(self, number: int) -> int | None:
        result = self._run_or_raise(
            [
                "gh",
                "api",
                f"repos/{self.repo}/issues/{number}",
                "--jq",
                ".parent.number",
            ],
            f"gh api repos/{self.repo}/issues/{number} failed",
        )
        output = self._decode(result.stdout)
        if not output or output == "null":
            return None
        try:
            return int(output)
        except ValueError:
            raise GithubCommandError(
                f"gh api repos/{self.repo}/issues/{number} returned unexpected output: {output!r}",
            ) from None

    def get_open_sub_issues(self, number: int) -> list[int]:
        data = self._get_all_sub_issues(number)
        return [
            int(str(item["number"])) for item in data if item.get("state") == "open"
        ]

    def has_open_issues_with_label(self, label: str) -> bool:
        result = self._run_or_raise(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--label",
                label,
                "--json",
                "number",
                "--jq",
                "length",
            ],
            f"gh issue list --label {label} failed",
        )
        output = self._decode(result.stdout)
        try:
            return int(output) > 0
        except ValueError:
            raise GithubCommandError(
                f"gh issue list --label {label} returned unexpected output: {output!r}",
            ) from None

    def get_issue_title(self, issue_number: int) -> str:
        result = self._run_or_raise(
            [
                "gh",
                "api",
                f"repos/{self.repo}/issues/{issue_number}",
                "--jq",
                ".title",
            ],
            f"gh api repos/{self.repo}/issues/{issue_number} failed",
        )
        return self._decode(result.stdout)

    def get_labels(self, issue_number: int) -> list[str]:
        result = self._run_or_raise(
            [
                "gh",
                "api",
                f"repos/{self.repo}/issues/{issue_number}",
                "--jq",
                ".labels[].name",
            ],
            f"gh api repos/{self.repo}/issues/{issue_number} failed",
        )
        output = self._decode(result.stdout)
        if not output:
            return []
        return output.splitlines()

    def close_issue_with_parents(self, number: int) -> None:
        self.close_issue(number)
        parent = self.get_parent(number)
        if parent is None:
            return
        open_siblings = self.get_open_sub_issues(parent)
        if open_siblings:
            return
        self.close_issue_with_parents(parent)

    def get_open_issue_numbers(self) -> list[int]:
        result = self._run_or_raise(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number",
                "--jq",
                ".[].number",
            ],
            "gh issue list failed",
        )
        output = self._decode(result.stdout)
        if not output:
            return []
        try:
            return [int(line) for line in output.splitlines()]
        except ValueError:
            raise GithubCommandError(
                f"gh issue list returned unexpected output: {output!r}",
            ) from None

    def _get_all_sub_issues(self, number: int) -> list[dict[str, object]]:
        result = self._run_or_raise(
            ["gh", "api", f"repos/{self.repo}/issues/{number}/sub_issues"],
            f"gh api repos/{self.repo}/issues/{number}/sub_issues failed",
        )
        try:
            return json.loads(result.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise GithubCommandError(
                f"gh api repos/{self.repo}/issues/{number}/sub_issues returned invalid JSON",
            ) from exc

    def get_open_issues(self, label: str) -> list[dict[str, object]]:
        result = self._run_or_raise(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--label",
                label,
                "--json",
                "number,title,body,labels,comments",
                "--jq",
                "[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]",
            ],
            f"gh issue list --label {label} failed",
        )
        try:
            return json.loads(result.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise GithubCommandError(
                f"gh issue list --label {label} returned invalid JSON",
            ) from exc

    def close_completed_parent_issues(self) -> None:
        changed = True
        while changed:
            changed = False
            for issue_num in self.get_open_issue_numbers():
                all_subs = self._get_all_sub_issues(issue_num)
                if all_subs and all(s.get("state") == "closed" for s in all_subs):
                    self.close_issue(issue_num)
                    changed = True
