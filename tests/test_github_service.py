from __future__ import annotations

import io
import json
import socket
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from pycastle.config import Config
from pycastle.services import (
    GithubAPIError,
    GithubAuthError,
    GithubNetworkError,
    GithubService,
    GithubServiceError,
)

_cfg = Config()


# ── Exception hierarchy ────────────────────────────────────────────────────────


def test_github_service_error_is_runtime_error():
    assert issubclass(GithubServiceError, RuntimeError)


def test_github_auth_error_is_github_service_error():
    assert issubclass(GithubAuthError, GithubServiceError)


def test_github_api_error_is_github_service_error():
    assert issubclass(GithubAPIError, GithubServiceError)


def test_github_network_error_is_github_service_error():
    assert issubclass(GithubNetworkError, GithubServiceError)


def test_github_auth_error_carries_status_and_body():
    err = GithubAuthError("msg", status=401, body="bad creds")
    assert err.status == 401
    assert err.body == "bad creds"


def test_github_api_error_carries_status_body_method_path():
    err = GithubAPIError("msg", status=500, body="boom", method="GET", path="/x")
    assert err.status == 500
    assert err.body == "boom"
    assert err.method == "GET"
    assert err.path == "/x"


def test_github_network_error_carries_cause():
    cause = URLError("dns failure")
    err = GithubNetworkError("msg", cause=cause)
    assert err.cause is cause


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_response(
    body: bytes | str = b"",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    if isinstance(body, str):
        body = body.encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.headers = headers or {}
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, exc_type, exc, tb: None
    return resp


def _make_http_error(status: int, body: bytes | str = b"") -> HTTPError:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return HTTPError(
        url="https://api.github.com/x",
        code=status,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def _make_service(
    repo: str = "owner/repo", token: str = "tkn", cfg: Config | None = None
) -> GithubService:
    return GithubService(repo, token, cfg or _cfg)


# ── Construction ───────────────────────────────────────────────────────────────


def test_constructor_accepts_repo_token_and_cfg():
    svc = GithubService("owner/repo", "tkn", Config(worktree_timeout=5))
    assert svc.repo == "owner/repo"


def test_github_service_does_not_inherit_from_subprocess_service():
    from pycastle.services._base import _SubprocessService

    assert not issubclass(GithubService, _SubprocessService)


# ── check_auth (tracer) ───────────────────────────────────────────────────────


def test_check_auth_returns_login_on_success():
    svc = _make_service()
    body = json.dumps({"login": "alice"}).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
    ):
        assert svc.check_auth() == "alice"


def test_check_auth_calls_get_user_endpoint():
    svc = _make_service()
    body = json.dumps({"login": "alice"}).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
    ) as m:
        svc.check_auth()
    req = m.call_args[0][0]
    assert req.full_url == "https://api.github.com/user"
    assert req.get_method() == "GET"


def test_check_auth_raises_github_auth_error_on_401():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        side_effect=_make_http_error(401, b'{"message":"Bad credentials"}'),
    ):
        with pytest.raises(GithubAuthError) as ei:
            svc.check_auth()
    assert ei.value.status == 401
    assert "Bad credentials" in ei.value.body


# ── _request: headers ─────────────────────────────────────────────────────────


def test_request_sets_authorization_bearer_header():
    svc = _make_service(token="abc123")
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(b"{}")
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer abc123"


def test_request_sets_accept_header():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(b"{}")
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    assert req.get_header("Accept") == "application/vnd.github+json"


def test_request_sets_api_version_header():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(b"{}")
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    assert req.get_header("X-github-api-version") == "2022-11-28"


def test_request_sets_user_agent_header_with_version():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(b"{}")
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    ua = req.get_header("User-agent")
    assert ua.startswith("pycastle/")


# ── _request: timeout ─────────────────────────────────────────────────────────


def test_request_passes_worktree_timeout_to_urlopen():
    svc = _make_service(cfg=Config(worktree_timeout=7))
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(b"{}")
    ) as m:
        svc._request("GET", "/x")
    assert m.call_args.kwargs.get("timeout") == 7


# ── _request: errors ─────────────────────────────────────────────────────────


def test_request_raises_github_api_error_on_non_401_4xx():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        side_effect=_make_http_error(404, b'{"message":"Not Found"}'),
    ):
        with pytest.raises(GithubAPIError) as ei:
            svc._request("GET", "/missing")
    assert ei.value.status == 404
    assert ei.value.method == "GET"
    assert ei.value.path == "/missing"
    assert "Not Found" in ei.value.body


def test_request_raises_github_api_error_on_5xx():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        side_effect=_make_http_error(500, b"server boom"),
    ):
        with pytest.raises(GithubAPIError) as ei:
            svc._request("GET", "/x")
    assert ei.value.status == 500


def test_request_raises_github_network_error_on_url_error():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        side_effect=URLError("dns fail"),
    ):
        with pytest.raises(GithubNetworkError):
            svc._request("GET", "/x")


def test_request_raises_github_network_error_on_socket_timeout():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        side_effect=socket.timeout("timed out"),
    ):
        with pytest.raises(GithubNetworkError):
            svc._request("GET", "/x")


# ── _request: success/decoding ────────────────────────────────────────────────


def test_request_returns_decoded_json_payload():
    svc = _make_service()
    body = json.dumps({"a": 1}).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
    ):
        payload, _ = svc._request("GET", "/x")
    assert payload == {"a": 1}


def test_request_returns_none_on_empty_body():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(b"")
    ):
        payload, _ = svc._request("DELETE", "/x")
    assert payload is None


def test_request_sends_json_body_when_data_provided():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(b"{}")
    ) as m:
        svc._request("POST", "/x", data={"hello": "world"})
    req = m.call_args[0][0]
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode()) == {"hello": "world"}
    assert req.get_header("Content-type") == "application/json"


# ── _paginate ────────────────────────────────────────────────────────────────


def test_paginate_returns_concatenated_results_across_pages():
    svc = _make_service()
    page1 = _make_response(
        json.dumps([{"n": 1}, {"n": 2}]).encode(),
        headers={
            "Link": '<https://api.github.com/x?page=2>; rel="next", '
            '<https://api.github.com/x?page=3>; rel="last"'
        },
    )
    page2 = _make_response(
        json.dumps([{"n": 3}]).encode(),
        headers={
            "Link": '<https://api.github.com/x?page=3>; rel="next", '
            '<https://api.github.com/x?page=1>; rel="prev"'
        },
    )
    page3 = _make_response(
        json.dumps([{"n": 4}]).encode(),
        headers={"Link": '<https://api.github.com/x?page=2>; rel="prev"'},
    )
    with patch(
        "pycastle.services.github_service.urlopen",
        side_effect=[page1, page2, page3],
    ):
        result = svc._paginate("/x")
    assert result == [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}]


def test_paginate_returns_single_page_when_no_link_header():
    svc = _make_service()
    page = _make_response(json.dumps([{"n": 1}]).encode(), headers={})
    with patch("pycastle.services.github_service.urlopen", return_value=page):
        result = svc._paginate("/x")
    assert result == [{"n": 1}]


def test_paginate_returns_single_page_when_link_has_no_next():
    svc = _make_service()
    page = _make_response(
        json.dumps([{"n": 1}]).encode(),
        headers={"Link": '<https://api.github.com/x?page=1>; rel="prev"'},
    )
    with patch("pycastle.services.github_service.urlopen", return_value=page):
        result = svc._paginate("/x")
    assert result == [{"n": 1}]


def test_paginate_follows_next_url_returned_by_github():
    svc = _make_service()
    next_url = "https://api.github.com/x?page=2&token=opaque"
    page1 = _make_response(
        json.dumps([1]).encode(),
        headers={"Link": f'<{next_url}>; rel="next"'},
    )
    page2 = _make_response(json.dumps([2]).encode(), headers={})
    captured: list[Any] = []

    def fake_urlopen(req: Any, timeout: float = 0) -> Any:
        captured.append(req.full_url)
        return page1 if len(captured) == 1 else page2

    with patch("pycastle.services.github_service.urlopen", side_effect=fake_urlopen):
        svc._paginate("/x")
    assert captured[0] == "https://api.github.com/x"
    assert captured[1] == next_url


# ── No real network ──────────────────────────────────────────────────────────


def test_no_real_network_call(monkeypatch):
    """Sanity: a misconfigured test must not be able to hit the network."""

    def boom(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr("pycastle.services.github_service.urlopen", boom)
    svc = _make_service()
    with pytest.raises(AssertionError):
        svc.check_auth()
