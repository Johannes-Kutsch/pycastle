from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.error import HTTPError, URLError
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


class GithubService:
    def __init__(self, repo: str, token: str, cfg: Config) -> None:
        self.repo = repo
        self._token = token
        self._timeout = cfg.worktree_timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _user_agent(),
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
                resp_headers = {k: v for k, v in resp.headers.items()}
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
