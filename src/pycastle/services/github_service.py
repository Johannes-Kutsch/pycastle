from __future__ import annotations

import json
import warnings
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..config import Config


class GithubServiceError(RuntimeError):
    pass


class GithubAuthError(GithubServiceError):
    def __init__(self, message: str, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(message)


class GithubAPIError(GithubServiceError):
    def __init__(
        self, message: str, status: int, body: str, method: str, path: str
    ) -> None:
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        super().__init__(message)


class GithubNetworkError(GithubServiceError):
    def __init__(self, message: str, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(message)


_API_BASE = "https://api.github.com"


def _user_agent() -> str:
    try:
        return f"pycastle/{version('pycastle')}"
    except PackageNotFoundError:
        return "pycastle/0.0.0"


_USER_AGENT = _user_agent()


class GithubService:
    def __init__(self, repo: str, token: str, cfg: Config) -> None:
        self.repo = repo
        self._token = token
        self._timeout = cfg.worktree_timeout
        self._recently_closed: set[int] = set()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _USER_AGENT,
        }

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else f"{_API_BASE}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                resp_headers = dict(resp.headers.items())
        except HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            status = exc.code
            if status == 401:
                raise GithubAuthError(
                    f"GitHub API {method} {path} returned 401: {err_body}",
                    status=status,
                    body=err_body,
                ) from exc
            raise GithubAPIError(
                f"GitHub API {method} {path} returned {status}: {err_body}",
                status=status,
                body=err_body,
                method=method,
                path=path,
            ) from exc
        except URLError as exc:
            raise GithubNetworkError(
                f"GitHub API {method} {path} transport error: {exc.reason}",
                cause=exc,
            ) from exc
        except TimeoutError as exc:
            raise GithubNetworkError(
                f"GitHub API {method} {path} timed out after {self._timeout}s",
                cause=exc,
            ) from exc
        if not raw:
            return None, resp_headers
        try:
            return json.loads(raw.decode("utf-8")), resp_headers
        except json.JSONDecodeError as exc:
            raise GithubAPIError(
                f"GitHub API {method} {path} returned invalid JSON",
                status=200,
                body=raw.decode("utf-8", errors="replace"),
                method=method,
                path=path,
            ) from exc

    def _paginate(self, path: str) -> list[Any]:
        results: list[Any] = []
        next_url: str | None = path
        while next_url is not None:
            payload, headers = self._request("GET", next_url)
            if not isinstance(payload, list):
                raise GithubAPIError(
                    f"GitHub API GET {next_url} expected list, got {type(payload).__name__}",
                    status=200,
                    body=str(payload),
                    method="GET",
                    path=next_url,
                )
            results.extend(payload)
            next_url = _next_link(headers.get("Link"))
        return results

    def check_auth(self) -> str:
        payload, _ = self._request("GET", "/user")
        if not isinstance(payload, dict) or "login" not in payload:
            raise GithubAPIError(
                "GitHub API GET /user returned no login",
                status=200,
                body=str(payload),
                method="GET",
                path="/user",
            )
        return str(payload["login"])

    def close_issue(self, number: int) -> None:
        try:
            self._request(
                "PATCH",
                f"/repos/{self.repo}/issues/{number}",
                data={"state": "closed"},
            )
        except GithubAPIError as exc:
            if exc.status not in (404, 410):
                raise
            warnings.warn(
                f"Issue #{number} is gone (HTTP {exc.status}); treating close as a no-op",
                UserWarning,
                stacklevel=2,
            )
        self._recently_closed.add(number)

    def get_issue(self, issue_number: int) -> dict[str, str | int | list]:
        path = f"/repos/{self.repo}/issues/{issue_number}"
        payload, _ = self._request("GET", path)
        if not isinstance(payload, dict) or "title" not in payload:
            raise GithubAPIError(
                f"GitHub API GET {path} returned no title",
                status=200,
                body=str(payload),
                method="GET",
                path=path,
            )
        raw_labels = payload.get("labels") or []
        labels = [
            str(lbl["name"])
            for lbl in raw_labels
            if isinstance(lbl, dict) and "name" in lbl
        ]
        return {
            "number": issue_number,
            "title": str(payload.get("title") or ""),
            "body": str(payload.get("body") or ""),
            "labels": labels,
            "comments": self.get_issue_comments(issue_number),
        }

    def get_issue_title(self, issue_number: int) -> str:
        return str(self.get_issue(issue_number)["title"])

    def get_issue_comments(self, issue_number: int) -> list[dict[str, str]]:
        results = self._paginate(
            f"/repos/{self.repo}/issues/{issue_number}/comments?per_page=100"
        )
        comments: list[dict[str, str]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            user = item.get("user") or {}
            author = str(user.get("login") or "") if isinstance(user, dict) else ""
            comments.append(
                {
                    "author": author,
                    "created_at": str(item.get("created_at") or ""),
                    "body": str(item.get("body") or ""),
                }
            )
        return comments

    def get_labels(self, issue_number: int) -> list[str]:
        payload, _ = self._request("GET", f"/repos/{self.repo}/issues/{issue_number}")
        if not isinstance(payload, dict):
            return []
        labels = payload.get("labels") or []
        return [str(label["name"]) for label in labels if "name" in label]

    def get_parent(self, number: int) -> int | None:
        payload, _ = self._request("GET", f"/repos/{self.repo}/issues/{number}")
        if not isinstance(payload, dict):
            return None
        parent = payload.get("parent")
        if not isinstance(parent, dict):
            return None
        parent_number = parent.get("number")
        if parent_number is None:
            return None
        return int(parent_number)

    def _get_all_sub_issues(self, number: int) -> list[dict[str, Any]]:
        results = self._paginate(f"/repos/{self.repo}/issues/{number}/sub_issues")
        return [item for item in results if isinstance(item, dict)]

    def get_open_sub_issues(self, number: int) -> list[int]:
        return [
            int(item["number"])
            for item in self._get_all_sub_issues(number)
            if item.get("state") == "open" and "number" in item
        ]

    def add_sub_issue(self, parent_number: int, child_number: int) -> None:
        self._request(
            "POST",
            f"/repos/{self.repo}/issues/{parent_number}/sub_issues",
            data={"sub_issue_id": child_number},
        )

    def close_issue_with_parents(self, number: int) -> None:
        self.close_issue(number)
        parent = self.get_parent(number)
        if parent is None:
            return
        if self.get_open_sub_issues(parent):
            return
        self.close_issue_with_parents(parent)

    def get_open_issue_numbers(self) -> list[int]:
        results = self._paginate(f"/repos/{self.repo}/issues?state=open&per_page=100")
        return [
            int(item["number"])
            for item in results
            if isinstance(item, dict)
            and "number" in item
            and "pull_request" not in item
        ]

    def _filter_open_issue_items(self, results: list[Any]) -> list[dict[str, Any]]:
        response_numbers = {
            int(item["number"])
            for item in results
            if isinstance(item, dict) and "number" in item
        }
        self._recently_closed = {
            n for n in self._recently_closed if n in response_numbers
        }
        return [
            item
            for item in results
            if isinstance(item, dict)
            and "pull_request" not in item
            and int(item["number"]) not in self._recently_closed
        ]

    @staticmethod
    def _extract_label_names(item: dict[str, Any]) -> list[str]:
        return [
            str(label_obj["name"])
            for label_obj in (item.get("labels") or [])
            if "name" in label_obj
        ]

    def get_open_issues(self, label: str) -> list[dict[str, Any]]:
        results = self._paginate(
            f"/repos/{self.repo}/issues?state=open"
            f"&labels={quote(label, safe='')}&per_page=100"
        )
        issues: list[dict[str, Any]] = []
        for item in self._filter_open_issue_items(results):
            number = int(item["number"])
            issues.append(
                {
                    "number": number,
                    "title": str(item.get("title") or ""),
                    "body": item.get("body") or "",
                    "labels": self._extract_label_names(item),
                    "comments": self.get_issue_comments(number)
                    if item.get("comments")
                    else [],
                }
            )
        return issues

    def get_all_open_issues_lightweight(self) -> list[dict[str, Any]]:
        results = self._paginate(f"/repos/{self.repo}/issues?state=open&per_page=100")
        return [
            {
                "number": int(item["number"]),
                "title": str(item.get("title") or ""),
                "labels": self._extract_label_names(item),
            }
            for item in self._filter_open_issue_items(results)
        ]

    def close_completed_parent_issues(self) -> None:
        changed = True
        while changed:
            changed = False
            for issue_num in self.get_open_issue_numbers():
                all_subs = self._get_all_sub_issues(issue_num)
                if all_subs and all(s.get("state") == "closed" for s in all_subs):
                    self.close_issue(issue_num)
                    changed = True

    def list_labels(self) -> list[dict[str, Any]]:
        results = self._paginate(f"/repos/{self.repo}/labels?per_page=100")
        return [item for item in results if isinstance(item, dict)]

    def create_label(self, body: dict[str, Any]) -> None:
        self._request("POST", f"/repos/{self.repo}/labels", data=body)

    def delete_label(self, name: str) -> None:
        self._request("DELETE", f"/repos/{self.repo}/labels/{quote(name, safe='')}")

    def add_label_to_issue(self, issue_number: int, label: str) -> None:
        self._request(
            "POST",
            f"/repos/{self.repo}/issues/{issue_number}/labels",
            data={"labels": [label]},
        )

    def remove_label_from_issue(self, issue_number: int, label: str) -> None:
        try:
            self._request(
                "DELETE",
                f"/repos/{self.repo}/issues/{issue_number}/labels/{quote(label, safe='')}",
            )
        except GithubAPIError as exc:
            if exc.status not in (404, 410):
                raise

    def post_comment(self, issue_number: int, body: str) -> None:
        self._request(
            "POST",
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            data={"body": body},
        )

    def create_issue_in(
        self, owner_repo: str, title: str, body: str, labels: list[str]
    ) -> int:
        payload, _ = self._request(
            "POST",
            f"/repos/{owner_repo}/issues",
            data={"title": title, "body": body, "labels": labels},
        )
        if not isinstance(payload, dict) or "number" not in payload:
            raise GithubAPIError(
                f"GitHub API POST /repos/{owner_repo}/issues returned no number",
                status=200,
                body=str(payload),
                method="POST",
                path=f"/repos/{owner_repo}/issues",
            )
        return int(payload["number"])


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if not segment.startswith("<"):
            continue
        end = segment.find(">")
        if end == -1:
            continue
        url = segment[1:end]
        params = segment[end + 1 :]
        if 'rel="next"' in params:
            return url
    return None
