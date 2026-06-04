from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_API_BASE = "https://api.github.com"


def _user_agent() -> str:
    try:
        return f"pycastle/{version('pycastle')}"
    except PackageNotFoundError:
        return "pycastle/0.0.0"


_USER_AGENT = _user_agent()


class GithubHttpTransportError(RuntimeError):
    pass


class GithubHttpTransportAuthError(GithubHttpTransportError):
    def __init__(self, message: str, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(message)


class GithubHttpTransportAPIError(GithubHttpTransportError):
    def __init__(
        self,
        message: str,
        status: int,
        body: str,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        self.headers = headers or {}
        super().__init__(message)


class GithubHttpTransportNetworkError(GithubHttpTransportError):
    def __init__(self, message: str, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(message)


class GithubHttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]: ...


class UrllibGithubHttpTransport:
    def __init__(self, token: str, timeout: int) -> None:
        self._token = token
        self._timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else f"{_API_BASE}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _USER_AGENT,
        }
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
            err_headers = dict(exc.headers.items()) if exc.headers is not None else {}
            if status == 401:
                raise GithubHttpTransportAuthError(
                    f"GitHub API {method} {path} returned 401: {err_body}",
                    status=status,
                    body=err_body,
                ) from exc
            raise GithubHttpTransportAPIError(
                f"GitHub API {method} {path} returned {status}: {err_body}",
                status=status,
                body=err_body,
                method=method,
                path=path,
                headers=err_headers,
            ) from exc
        except URLError as exc:
            raise GithubHttpTransportNetworkError(
                f"GitHub API {method} {path} transport error: {exc.reason}",
                cause=exc,
            ) from exc
        except TimeoutError as exc:
            raise GithubHttpTransportNetworkError(
                f"GitHub API {method} {path} timed out after {self._timeout}s",
                cause=exc,
            ) from exc
        if not raw:
            return None, resp_headers
        try:
            return json.loads(raw.decode("utf-8")), resp_headers
        except json.JSONDecodeError as exc:
            raise GithubHttpTransportAPIError(
                f"GitHub API {method} {path} returned invalid JSON",
                status=200,
                body=raw.decode("utf-8", errors="replace"),
                method=method,
                path=path,
                headers=resp_headers,
            ) from exc
