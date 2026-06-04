from __future__ import annotations

import io
import json
import socket
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from pycastle.config import Config
from pycastle.services import (
    GithubAPIError,
    GithubAuthError,
    GithubNetworkError,
    OperatorActionableGithubError,
    GithubService,
    GithubServiceError,
)
from pycastle.services._github_http_transport import (
    GithubHttpTransportAPIError,
    GithubHttpTransportAuthError,
    GithubHttpTransportNetworkError,
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


def _make_http_error(
    status: int,
    body: bytes | str = b"",
    headers: dict[str, str] | None = None,
) -> HTTPError:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return HTTPError(
        url="https://api.github.com/x",
        code=status,
        msg="err",
        hdrs=headers,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def _make_service(
    repo: str = "owner/repo",
    token: str = "tkn",
    cfg: Config | None = None,
    transport: Any | None = None,
) -> GithubService:
    return GithubService(repo, token, cfg or _cfg, transport=transport)


class _FakeGithubTransport:
    def __init__(self, request_fn: Any) -> None:
        self._request_fn = request_fn
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        self.calls.append((method, path, data))
        return self._request_fn(method, path, data)


@dataclass(frozen=True)
class _GithubTransportRequest:
    method: str
    path: str
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class _GithubTransportReply:
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    error: BaseException | None = None


@dataclass(frozen=True)
class _GithubTransportStep:
    request: _GithubTransportRequest
    reply: _GithubTransportReply = field(default_factory=_GithubTransportReply)


class _ScriptedGithubTransport:
    def __init__(self, steps: list[_GithubTransportStep]) -> None:
        self._remaining = list(steps)
        self.requests: list[_GithubTransportRequest] = []

    def request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        request = _GithubTransportRequest(method=method, path=path, data=data)
        self.requests.append(request)
        if not self._remaining:
            raise AssertionError(f"unexpected transport request: {request}")

        step = self._remaining.pop(0)
        assert request == step.request

        if step.reply.error is not None:
            raise step.reply.error
        return step.reply.payload, step.reply.headers

    def assert_exhausted(self) -> None:
        assert self._remaining == []


def _script_step(
    method: str,
    path: str,
    *,
    data: dict[str, Any] | None = None,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    error: BaseException | None = None,
) -> _GithubTransportStep:
    return _GithubTransportStep(
        request=_GithubTransportRequest(method=method, path=path, data=data),
        reply=_GithubTransportReply(
            payload=payload,
            headers=headers or {},
            error=error,
        ),
    )


# ── Construction ───────────────────────────────────────────────────────────────


def test_constructor_accepts_repo_token_and_cfg():
    svc = GithubService("owner/repo", "tkn", Config(worktree_timeout=5))
    assert svc.repo == "owner/repo"


def test_github_service_does_not_inherit_from_subprocess_service():
    from pycastle.services._base import _SubprocessService

    assert not issubclass(GithubService, _SubprocessService)


# ── check_auth (tracer) ───────────────────────────────────────────────────────


def test_check_auth_with_scripted_transport_returns_authenticated_login():
    transport = _ScriptedGithubTransport(
        [_script_step("GET", "/user", payload={"login": "alice"})]
    )
    svc = _make_service(transport=transport)

    assert svc.check_auth() == "alice"
    assert transport.requests == [_GithubTransportRequest("GET", "/user", None)]
    transport.assert_exhausted()


def test_check_auth_uses_injected_transport_adapter():
    transport = _FakeGithubTransport(
        lambda method, path, data: ({"login": "alice"}, {})
    )
    svc = _make_service(transport=transport)

    assert svc.check_auth() == "alice"
    assert transport.calls == [("GET", "/user", None)]


def test_check_auth_with_scripted_transport_raises_api_error_when_login_missing():
    transport = _ScriptedGithubTransport(
        [_script_step("GET", "/user", payload={"id": 1})]
    )
    svc = _make_service(transport=transport)

    with pytest.raises(GithubAPIError) as exc_info:
        svc.check_auth()

    assert exc_info.value.status == 200
    assert exc_info.value.body == "{'id': 1}"
    assert exc_info.value.method == "GET"
    assert exc_info.value.path == "/user"
    assert transport.requests == [_GithubTransportRequest("GET", "/user", None)]
    transport.assert_exhausted()


def test_check_auth_with_scripted_transport_bypasses_no_real_network_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("real network call attempted")

    monkeypatch.setattr("pycastle.services._github_http_transport.urlopen", boom)
    transport = _ScriptedGithubTransport(
        [_script_step("GET", "/user", payload={"login": "alice"})]
    )
    svc = _make_service(transport=transport)

    assert svc.check_auth() == "alice"
    assert transport.requests == [_GithubTransportRequest("GET", "/user", None)]
    transport.assert_exhausted()


def test_check_auth_calls_get_user_endpoint():
    svc = _make_service()
    body = json.dumps({"login": "alice"}).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ) as m:
        svc.check_auth()
    req = m.call_args[0][0]
    assert req.full_url == "https://api.github.com/user"
    assert req.get_method() == "GET"


def test_check_auth_with_scripted_transport_raises_github_auth_error_on_401():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/user",
                error=GithubHttpTransportAuthError(
                    "bad creds",
                    status=401,
                    body='{"message":"Bad credentials"}',
                ),
            )
        ]
    )
    svc = _make_service(transport=transport)

    with pytest.raises(GithubAuthError) as exc_info:
        svc.check_auth()

    assert exc_info.value.status == 401
    assert exc_info.value.body == '{"message":"Bad credentials"}'
    assert transport.requests == [_GithubTransportRequest("GET", "/user", None)]
    transport.assert_exhausted()


def test_check_auth_retries_transient_5xx_and_recovers():
    svc = _make_service()
    body = json.dumps({"login": "alice"}).encode()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[
                _make_http_error(500, b"server boom"),
                _make_response(body),
            ],
        ),
        patch("time.sleep") as mock_sleep,
    ):
        assert svc.check_auth() == "alice"

    mock_sleep.assert_called_once_with(10)


def test_check_auth_uses_retry_after_header_when_present():
    svc = _make_service()
    body = json.dumps({"login": "alice"}).encode()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[
                _make_http_error(
                    429,
                    b'{"message":"secondary rate limit"}',
                    headers={"Retry-After": "6"},
                ),
                _make_response(body),
            ],
        ),
        patch("time.sleep") as mock_sleep,
    ):
        assert svc.check_auth() == "alice"

    mock_sleep.assert_called_once_with(6)


# ── _request: headers ─────────────────────────────────────────────────────────


def test_request_sets_authorization_bearer_header():
    svc = _make_service(token="abc123")
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b"{}"),
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer abc123"


def test_request_sets_accept_header():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b"{}"),
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    assert req.get_header("Accept") == "application/vnd.github+json"


def test_request_sets_api_version_header():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b"{}"),
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    assert req.get_header("X-github-api-version") == "2022-11-28"


def test_request_sets_user_agent_header_with_version():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b"{}"),
    ) as m:
        svc._request("GET", "/x")
    req = m.call_args[0][0]
    ua = req.get_header("User-agent")
    assert ua.startswith("pycastle/")


# ── _request: timeout ─────────────────────────────────────────────────────────


def test_request_passes_worktree_timeout_to_urlopen():
    svc = _make_service(cfg=Config(worktree_timeout=7))
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b"{}"),
    ) as m:
        svc._request("GET", "/x")
    assert m.call_args.kwargs.get("timeout") == 7


# ── _request: errors ─────────────────────────────────────────────────────────


def test_request_raises_github_api_error_on_non_401_4xx():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        side_effect=_make_http_error(404, b'{"message":"Not Found"}'),
    ):
        with pytest.raises(GithubAPIError) as ei:
            svc._request("GET", "/missing")
    assert ei.value.status == 404
    assert ei.value.method == "GET"
    assert ei.value.path == "/missing"
    assert "Not Found" in ei.value.body


def test_check_auth_with_scripted_transport_raises_github_api_error_on_http_failure():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/user",
                error=GithubHttpTransportAPIError(
                    "boom",
                    status=404,
                    body="not found",
                    method="GET",
                    path="/user",
                    headers={"X-Test": "1"},
                ),
            )
        ]
    )
    svc = _make_service(transport=transport)

    with pytest.raises(GithubAPIError) as exc_info:
        svc.check_auth()

    assert exc_info.value.status == 404
    assert exc_info.value.body == "not found"
    assert exc_info.value.method == "GET"
    assert exc_info.value.path == "/user"
    assert exc_info.value.headers == {"X-Test": "1"}
    assert transport.requests == [_GithubTransportRequest("GET", "/user", None)]
    transport.assert_exhausted()


def test_request_post_raises_github_api_error_on_5xx_without_retry():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        side_effect=_make_http_error(500, b"server boom"),
    ):
        with pytest.raises(GithubAPIError) as ei:
            svc._request("POST", "/x")
    assert ei.value.status == 500


def test_request_post_raises_github_network_error_on_url_error():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        side_effect=URLError("dns fail"),
    ):
        with pytest.raises(GithubNetworkError):
            svc._request("POST", "/x")


def test_request_post_raises_github_network_error_on_socket_timeout():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        side_effect=socket.timeout("timed out"),
    ):
        with pytest.raises(GithubNetworkError):
            svc._request("POST", "/x")


# ── _request: success/decoding ────────────────────────────────────────────────


def test_request_returns_decoded_json_payload():
    svc = _make_service()
    body = json.dumps({"a": 1}).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ):
        payload, _ = svc._request("GET", "/x")
    assert payload == {"a": 1}


def test_request_returns_none_on_empty_body():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b""),
    ):
        payload, _ = svc._request("DELETE", "/x")
    assert payload is None


def test_request_sends_json_body_when_data_provided():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b"{}"),
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
        "pycastle.services._github_http_transport.urlopen",
        side_effect=[page1, page2, page3],
    ):
        result = svc._paginate("/x")
    assert result == [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}]


def test_paginate_returns_single_page_when_no_link_header():
    svc = _make_service()
    page = _make_response(json.dumps([{"n": 1}]).encode(), headers={})
    with patch("pycastle.services._github_http_transport.urlopen", return_value=page):
        result = svc._paginate("/x")
    assert result == [{"n": 1}]


def test_paginate_returns_single_page_when_link_has_no_next():
    svc = _make_service()
    page = _make_response(
        json.dumps([{"n": 1}]).encode(),
        headers={"Link": '<https://api.github.com/x?page=1>; rel="prev"'},
    )
    with patch("pycastle.services._github_http_transport.urlopen", return_value=page):
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

    with patch(
        "pycastle.services._github_http_transport.urlopen", side_effect=fake_urlopen
    ):
        svc._paginate("/x")
    assert captured[0] == "https://api.github.com/x"
    assert captured[1] == next_url


# ── close_issue ──────────────────────────────────────────────────────────────


def test_close_issue_sends_patch_with_state_closed():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b'{"state":"closed"}'),
    ) as m:
        svc.close_issue(42)
    req = m.call_args[0][0]
    assert req.get_method() == "PATCH"
    assert req.full_url == "https://api.github.com/repos/owner/repo/issues/42"
    assert json.loads(req.data.decode()) == {"state": "closed"}


def test_close_issue_raises_operator_actionable_error_after_retry_exhaustion():
    svc = _make_service()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[_make_http_error(500, b"server error")] * 4,
        ),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(OperatorActionableGithubError) as exc_info:
            svc.close_issue(42)

    assert exc_info.value.method == "PATCH"
    assert exc_info.value.path == "/repos/owner/repo/issues/42"
    assert exc_info.value.attempt_count == 4
    assert isinstance(exc_info.value.cause, GithubAPIError)
    assert [call.args[0] for call in mock_sleep.call_args_list] == [10, 60, 300]


def test_close_issue_retries_transient_5xx_and_recovers():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/42",
                data={"state": "closed"},
                error=GithubHttpTransportAPIError(
                    "boom",
                    status=500,
                    body="server boom",
                    method="PATCH",
                    path="/repos/owner/repo/issues/42",
                ),
            ),
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/42",
                data={"state": "closed"},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    with patch("time.sleep") as mock_sleep:
        svc.close_issue(42)

    mock_sleep.assert_called_once_with(10)
    assert transport.requests == [
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/42", {"state": "closed"}
        ),
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/42", {"state": "closed"}
        ),
    ]


@pytest.mark.parametrize("status", [404, 410])
def test_close_issue_treats_gone_as_no_op_with_warning(status):
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        side_effect=_make_http_error(status, b'{"message":"gone"}'),
    ):
        with pytest.warns(UserWarning, match="42"):
            svc.close_issue(42)


def test_close_issue_on_410_marks_issue_as_closed_for_get_open_issues():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/99",
                data={"state": "closed"},
                error=GithubHttpTransportAPIError(
                    "gone",
                    status=410,
                    body='{"message":"This issue was deleted"}',
                    method="PATCH",
                    path="/repos/owner/repo/issues/99",
                ),
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100",
                payload=[{"number": 99, "title": "gone", "body": "", "labels": []}],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    with pytest.warns(UserWarning):
        svc.close_issue(99)
    open_issues = svc.get_open_issues("ready-for-agent")

    assert all(i["number"] != 99 for i in open_issues)


# ── get_issue ────────────────────────────────────────────────────────────────


def test_get_issue_with_scripted_transport_returns_number_title_body_labels_and_comments():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues/7",
                payload={
                    "number": 7,
                    "title": "Fix bug",
                    "body": "do it",
                    "labels": [{"name": "bug"}, {"name": "ready-for-agent"}],
                },
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/7/comments?per_page=100",
                payload=[
                    {
                        "user": {"login": "alice"},
                        "created_at": "2024-01-01T00:00:00Z",
                        "body": "LGTM",
                    }
                ],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_issue(7)

    assert result == {
        "number": 7,
        "title": "Fix bug",
        "body": "do it",
        "labels": ["bug", "ready-for-agent"],
        "comments": [
            {
                "author": "alice",
                "created_at": "2024-01-01T00:00:00Z",
                "body": "LGTM",
            }
        ],
    }
    transport.assert_exhausted()


@pytest.mark.parametrize(
    "payload",
    [
        {"number": 7, "title": "Fix bug", "body": None},
        {"number": 7, "title": "Fix bug"},
    ],
)
def test_get_issue_with_scripted_transport_returns_empty_string_for_null_or_missing_body(
    payload: dict[str, Any],
) -> None:
    transport = _ScriptedGithubTransport(
        [
            _script_step("GET", "/repos/owner/repo/issues/7", payload=payload),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/7/comments?per_page=100",
                payload=[],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_issue(7)

    assert result["body"] == ""
    transport.assert_exhausted()


def test_get_issue_with_scripted_transport_raises_when_title_missing():
    transport = _ScriptedGithubTransport(
        [_script_step("GET", "/repos/owner/repo/issues/7", payload={"number": 7})]
    )
    svc = _make_service(transport=transport)

    with pytest.raises(GithubAPIError):
        svc.get_issue(7)

    transport.assert_exhausted()


def test_get_issue_with_scripted_transport_ignores_malformed_labels_and_projects_comments():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues/7",
                payload={
                    "number": 7,
                    "title": "Fix bug",
                    "body": "do it",
                    "labels": [{"name": "bug"}, 123, {"id": 99}],
                },
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/7/comments?per_page=100",
                payload=[
                    {
                        "user": {"login": "alice"},
                        "created_at": "2024-01-01T00:00:00Z",
                        "body": "LGTM",
                    }
                ],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_issue(7)

    assert result == {
        "number": 7,
        "title": "Fix bug",
        "body": "do it",
        "labels": ["bug"],
        "comments": [
            {
                "author": "alice",
                "created_at": "2024-01-01T00:00:00Z",
                "body": "LGTM",
            }
        ],
    }
    transport.assert_exhausted()


# ── get_issue_title / get_labels ─────────────────────────────────────────────


def test_get_issue_title_returns_title():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        side_effect=[
            _make_response(json.dumps({"number": 7, "title": "Fix bug"}).encode()),
            _make_response(json.dumps([]).encode()),
        ],
    ):
        assert svc.get_issue_title(7) == "Fix bug"


def test_get_issue_title_returns_title_when_body_key_absent():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        side_effect=[
            _make_response(json.dumps({"number": 7, "title": "Fix bug"}).encode()),
            _make_response(json.dumps([]).encode()),
        ],
    ):
        assert svc.get_issue_title(7) == "Fix bug"


def test_get_issue_title_retries_transient_5xx_and_recovers():
    svc = _make_service()
    issue_body = json.dumps({"number": 7, "title": "Fix bug"}).encode()
    comments_body = json.dumps([]).encode()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[
                _make_http_error(500, b"server boom"),
                _make_response(issue_body),
                _make_response(comments_body, headers={}),
            ],
        ),
        patch("time.sleep") as mock_sleep,
    ):
        assert svc.get_issue_title(7) == "Fix bug"

    mock_sleep.assert_called_once_with(10)


def test_get_issue_title_uses_retry_after_header_when_present():
    svc = _make_service()
    issue_body = json.dumps({"number": 7, "title": "Fix bug"}).encode()
    comments_body = json.dumps([]).encode()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[
                _make_http_error(
                    429,
                    b'{"message":"secondary rate limit"}',
                    headers={"Retry-After": "7"},
                ),
                _make_response(issue_body),
                _make_response(comments_body, headers={}),
            ],
        ),
        patch("time.sleep") as mock_sleep,
    ):
        assert svc.get_issue_title(7) == "Fix bug"

    mock_sleep.assert_called_once_with(7)


def test_get_issue_title_falls_back_to_exponential_retry_when_retry_after_is_malformed():
    svc = _make_service()
    issue_body = json.dumps({"number": 7, "title": "Fix bug"}).encode()
    comments_body = json.dumps([]).encode()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[
                _make_http_error(
                    429,
                    b'{"message":"secondary rate limit"}',
                    headers={"Retry-After": "not-a-delay"},
                ),
                _make_response(issue_body),
                _make_response(comments_body, headers={}),
            ],
        ),
        patch("time.sleep") as mock_sleep,
    ):
        assert svc.get_issue_title(7) == "Fix bug"

    mock_sleep.assert_called_once_with(10)


def test_get_issue_title_retries_rate_limited_403_from_headers():
    svc = _make_service()
    issue_body = json.dumps({"number": 7, "title": "Fix bug"}).encode()
    comments_body = json.dumps([]).encode()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[
                _make_http_error(
                    403,
                    b'{"message":"Forbidden"}',
                    headers={"X-RateLimit-Remaining": "0"},
                ),
                _make_response(issue_body),
                _make_response(comments_body, headers={}),
            ],
        ),
        patch("time.sleep") as mock_sleep,
    ):
        assert svc.get_issue_title(7) == "Fix bug"

    mock_sleep.assert_called_once_with(10)


def test_get_issue_title_raises_operator_actionable_error_after_retry_exhaustion():
    svc = _make_service()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[_make_http_error(500, b"server boom")] * 4,
        ),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(OperatorActionableGithubError) as exc_info:
            svc.get_issue_title(7)

    assert exc_info.value.method == "GET"
    assert exc_info.value.path == "/repos/owner/repo/issues/7"
    assert exc_info.value.attempt_count == 4
    assert isinstance(exc_info.value.cause, GithubAPIError)
    assert [call.args[0] for call in mock_sleep.call_args_list] == [10, 60, 300]


def test_get_issue_title_retries_transport_error_and_recovers():
    svc = _make_service()
    issue_body = json.dumps({"number": 7, "title": "Fix bug"}).encode()
    comments_body = json.dumps([]).encode()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[
                URLError("dns fail"),
                _make_response(issue_body),
                _make_response(comments_body, headers={}),
            ],
        ),
        patch("time.sleep") as mock_sleep,
    ):
        assert svc.get_issue_title(7) == "Fix bug"

    mock_sleep.assert_called_once_with(10)


def test_get_issue_title_does_not_retry_stable_403():
    svc = _make_service()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=_make_http_error(
                403,
                b'{"message":"Resource not accessible by personal access token"}',
            ),
        ),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(GithubAPIError) as exc_info:
            svc.get_issue_title(7)

    assert exc_info.value.status == 403
    mock_sleep.assert_not_called()


def test_get_labels_returns_label_names():
    svc = _make_service()
    body = json.dumps(
        {"labels": [{"name": "bug"}, {"name": "ready-for-agent"}]}
    ).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ):
        assert svc.get_labels(7) == ["bug", "ready-for-agent"]


def test_get_labels_returns_empty_list_when_no_labels():
    svc = _make_service()
    body = json.dumps({"labels": []}).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ):
        assert svc.get_labels(7) == []


def test_get_recent_improve_prds_returns_newest_12_canonical_titles_across_states():
    svc = _make_service()
    issues = [
        {
            "number": number,
            "title": f"[improve-PRD] Candidate {number}",
            "state": "open" if number % 2 else "closed",
        }
        for number in range(30, 17, -1)
    ]
    issues.insert(
        3,
        {
            "number": 999,
            "title": "Follow-up [improve-PRD] mention only",
            "state": "open",
        },
    )
    issues.insert(
        7,
        {
            "number": 998,
            "title": "[improve-SLICE] Not a PRD",
            "state": "closed",
        },
    )

    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(json.dumps(issues).encode(), headers={}),
    ):
        result = svc.get_recent_improve_prds()

    assert result == [
        {
            "number": number,
            "state": "OPEN" if number % 2 else "CLOSED",
            "title": f"Candidate {number}",
        }
        for number in range(30, 18, -1)
    ]


def test_get_recent_improve_prds_returns_empty_list_when_no_matching_issues():
    svc = _make_service()
    issues = [
        {"number": 1, "title": "Regular issue", "state": "open"},
        {"number": 2, "title": "Follow-up [improve-PRD] mention", "state": "closed"},
    ]
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(json.dumps(issues).encode()),
    ):
        assert svc.get_recent_improve_prds() == []


def test_get_recent_improve_prds_raises_operator_actionable_error_after_retry_exhaustion():
    svc = _make_service()
    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=[_make_http_error(500, b"server error")] * 4,
        ),
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(OperatorActionableGithubError) as exc_info:
            svc.get_recent_improve_prds()

    assert exc_info.value.path == "/repos/owner/repo/issues?state=all&per_page=100"
    assert [call.args[0] for call in mock_sleep.call_args_list] == [10, 60, 300]


# ── get_parent ───────────────────────────────────────────────────────────────


def test_get_parent_returns_parent_number():
    svc = _make_service()
    body = json.dumps({"parent": {"number": 100}}).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ):
        assert svc.get_parent(5) == 100


def test_get_parent_returns_none_when_no_parent():
    svc = _make_service()
    body = json.dumps({"number": 5}).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ):
        assert svc.get_parent(5) is None


# ── get_open_sub_issues ──────────────────────────────────────────────────────


def test_get_open_sub_issues_filters_to_open_only():
    svc = _make_service()
    body = json.dumps(
        [
            {"number": 1, "state": "open"},
            {"number": 2, "state": "closed"},
            {"number": 3, "state": "open"},
        ]
    ).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        assert svc.get_open_sub_issues(10) == [1, 3]


# ── add_sub_issue ────────────────────────────────────────────────────────────


def test_add_sub_issue_posts_to_correct_endpoint():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b""),
    ) as m:
        svc.add_sub_issue(parent_number=10, child_number=20)
    req = m.call_args[0][0]
    assert "/repos/owner/repo/issues/10/sub_issues" in req.full_url


def test_add_sub_issue_sends_child_as_sub_issue_id():
    svc = _make_service()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(b""),
    ) as m:
        svc.add_sub_issue(parent_number=10, child_number=20)
    req = m.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"sub_issue_id": 20}


# ── get_issue_comments ───────────────────────────────────────────────────────


def test_get_issue_comments_returns_author_created_at_and_body():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues/7/comments?per_page=100",
                payload=[
                    {
                        "user": {"login": "alice"},
                        "created_at": "2026-01-01T10:00:00Z",
                        "body": "first comment",
                    },
                    "skip",
                    {
                        "user": {"login": "bob"},
                        "created_at": "2026-01-02T10:00:00Z",
                        "body": "second comment",
                    },
                ],
                headers={"Link": ""},
            )
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_issue_comments(7)

    assert result == [
        {
            "author": "alice",
            "created_at": "2026-01-01T10:00:00Z",
            "body": "first comment",
        },
        {
            "author": "bob",
            "created_at": "2026-01-02T10:00:00Z",
            "body": "second comment",
        },
    ]
    transport.assert_exhausted()


def test_get_issue_comments_returns_empty_list_when_no_comments():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues/7/comments?per_page=100",
                payload=[],
                headers={"Link": ""},
            )
        ]
    )
    svc = _make_service(transport=transport)

    assert svc.get_issue_comments(7) == []
    transport.assert_exhausted()


def test_get_issue_comments_hits_comments_endpoint():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues/42/comments?per_page=100",
                payload=[],
                headers={"Link": ""},
            )
        ]
    )
    svc = _make_service(transport=transport)

    svc.get_issue_comments(42)

    assert transport.requests == [
        _GithubTransportRequest(
            "GET",
            "/repos/owner/repo/issues/42/comments?per_page=100",
            None,
        )
    ]
    transport.assert_exhausted()


# ── close_issue_with_parents ─────────────────────────────────────────────────


def test_close_issue_with_parents_closes_parent_when_no_open_siblings():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/5",
                data={"state": "closed"},
            ),
            _script_step(
                "GET", "/repos/owner/repo/issues/5", payload={"parent": {"number": 50}}
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/50/sub_issues",
                payload=[{"number": 5, "state": "closed"}],
                headers={"Link": ""},
            ),
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/50",
                data={"state": "closed"},
            ),
            _script_step(
                "GET", "/repos/owner/repo/issues/50", payload={"parent": None}
            ),
        ]
    )
    svc = _make_service(transport=transport)

    svc.close_issue_with_parents(5)

    assert transport.requests == [
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/5", {"state": "closed"}
        ),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/5", None),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/50/sub_issues", None),
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/50", {"state": "closed"}
        ),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/50", None),
    ]


def test_close_issue_with_parents_stops_when_open_siblings_remain():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/5",
                data={"state": "closed"},
            ),
            _script_step(
                "GET", "/repos/owner/repo/issues/5", payload={"parent": {"number": 50}}
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/50/sub_issues",
                payload=[
                    {"number": 5, "state": "closed"},
                    {"number": 6, "state": "open"},
                ],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    svc.close_issue_with_parents(5)

    assert transport.requests == [
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/5", {"state": "closed"}
        ),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/5", None),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/50/sub_issues", None),
    ]


def test_close_issue_with_parents_ignores_just_closed_child_when_checking_siblings():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/5",
                data={"state": "closed"},
            ),
            _script_step(
                "GET", "/repos/owner/repo/issues/5", payload={"parent": {"number": 50}}
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/50/sub_issues",
                payload=[{"number": 5, "state": "open"}],
                headers={"Link": ""},
            ),
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/50",
                data={"state": "closed"},
            ),
            _script_step(
                "GET", "/repos/owner/repo/issues/50", payload={"parent": None}
            ),
        ]
    )
    svc = _make_service(transport=transport)

    svc.close_issue_with_parents(5)

    assert transport.requests == [
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/5", {"state": "closed"}
        ),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/5", None),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/50/sub_issues", None),
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/50", {"state": "closed"}
        ),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/50", None),
    ]
    transport.assert_exhausted()


# ── get_open_issue_numbers ───────────────────────────────────────────────────


def test_get_open_issue_numbers_excludes_pull_requests():
    svc = _make_service()
    body = json.dumps(
        [
            {"number": 1},
            {"number": 2, "pull_request": {"url": "x"}},
            {"number": 3},
        ]
    ).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        assert svc.get_open_issue_numbers() == [1, 3]


# ── get_open_issues ──────────────────────────────────────────────────────────


def test_get_open_issues_with_scripted_transport_returns_projection_and_comment_loading():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&labels=bug&per_page=100",
                payload=[
                    {
                        "number": 1,
                        "title": "Fix",
                        "body": "do it",
                        "labels": [{"name": "bug"}, {"id": 1}],
                        "comments": 0,
                    },
                    {
                        "number": 2,
                        "title": "Discuss",
                        "body": "details",
                        "labels": [{"name": "bug"}],
                        "comments": 1,
                    },
                ],
                headers={"Link": ""},
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/2/comments?per_page=100",
                payload=[
                    {
                        "user": {"login": "alice"},
                        "created_at": "2026-01-01T00:00:00Z",
                        "body": "hi",
                    }
                ],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_open_issues("bug")

    assert result == [
        {
            "number": 1,
            "title": "Fix",
            "body": "do it",
            "labels": ["bug"],
            "comments": [],
        },
        {
            "number": 2,
            "title": "Discuss",
            "body": "details",
            "labels": ["bug"],
            "comments": [
                {
                    "author": "alice",
                    "created_at": "2026-01-01T00:00:00Z",
                    "body": "hi",
                }
            ],
        },
    ]
    transport.assert_exhausted()


def test_get_open_issues_filters_pull_requests():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&labels=bug&per_page=100",
                payload=[
                    {"number": 1, "title": "Issue", "body": "", "labels": []},
                    {
                        "number": 2,
                        "title": "PR",
                        "body": "",
                        "labels": [],
                        "pull_request": {"url": "x"},
                    },
                ],
                headers={"Link": ""},
            )
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_open_issues("bug")

    assert [r["number"] for r in result] == [1]
    transport.assert_exhausted()


def test_get_open_issues_filters_recently_closed_issue():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/42",
                data={"state": "closed"},
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100",
                payload=[{"number": 42, "title": "T", "body": "", "labels": []}],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    svc.close_issue(42)
    result = svc.get_open_issues("ready-for-agent")

    assert [r["number"] for r in result] == []


def test_get_open_issues_does_not_filter_after_self_heal_reopen():
    list_path = (
        "/repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100"
    )
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/42",
                data={"state": "closed"},
            ),
            _script_step(
                "GET",
                list_path,
                payload=[{"number": 42, "title": "T", "body": "", "labels": []}],
                headers={"Link": ""},
            ),
            _script_step("GET", list_path, payload=[], headers={"Link": ""}),
            _script_step(
                "GET",
                list_path,
                payload=[{"number": 42, "title": "T", "body": "", "labels": []}],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    svc.close_issue(42)
    svc.get_open_issues("ready-for-agent")
    svc.get_open_issues("ready-for-agent")
    result = svc.get_open_issues("ready-for-agent")

    assert [r["number"] for r in result] == [42]


def test_get_open_issues_does_not_filter_issue_when_close_failed():
    transport = _ScriptedGithubTransport(
        [
            *[
                _script_step(
                    "PATCH",
                    "/repos/owner/repo/issues/42",
                    data={"state": "closed"},
                    error=GithubHttpTransportAPIError(
                        "fail",
                        status=500,
                        body="err",
                        method="PATCH",
                        path="/repos/owner/repo/issues/42",
                    ),
                )
                for _ in range(4)
            ],
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&labels=ready-for-agent&per_page=100",
                payload=[{"number": 42, "title": "T", "body": "", "labels": []}],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    with patch("time.sleep"):
        with pytest.raises(OperatorActionableGithubError):
            svc.close_issue(42)
    result = svc.get_open_issues("ready-for-agent")

    assert [r["number"] for r in result] == [42]


def test_get_open_issues_with_scripted_transport_normalizes_mixed_open_issue_payload():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&labels=bug&per_page=100",
                payload=[
                    "skip",
                    {"title": "missing number"},
                    {
                        "number": "1",
                        "title": None,
                        "body": None,
                        "labels": [{"name": "bug"}],
                        "comments": 0,
                    },
                    {
                        "number": 2,
                        "title": "PR",
                        "body": "",
                        "labels": [],
                        "comments": 3,
                        "pull_request": {"url": "x"},
                    },
                    {
                        "number": "3",
                        "title": "Has comments",
                        "body": "details",
                        "labels": [{"name": "feat"}],
                        "comments": 1,
                    },
                ],
                headers={"Link": ""},
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/3/comments?per_page=100",
                payload=[
                    {
                        "user": {"login": "alice"},
                        "created_at": "2026-01-01T00:00:00Z",
                        "body": "hi",
                    }
                ],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_open_issues("bug")

    assert result == [
        {
            "number": 1,
            "title": "",
            "body": "",
            "labels": ["bug"],
            "comments": [],
        },
        {
            "number": 3,
            "title": "Has comments",
            "body": "details",
            "labels": ["feat"],
            "comments": [
                {
                    "author": "alice",
                    "created_at": "2026-01-01T00:00:00Z",
                    "body": "hi",
                }
            ],
        },
    ]
    transport.assert_exhausted()


# ── get_all_open_issues_lightweight ─────────────────────────────────────────


def test_get_all_open_issues_lightweight_returns_number_title_labels():
    svc = _make_service()
    body = json.dumps(
        [
            {
                "number": 1,
                "title": "Fix it",
                "body": "should be ignored",
                "labels": [{"name": "bug"}],
            }
        ]
    ).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        result = svc.get_all_open_issues_lightweight()
    assert result == [{"number": 1, "title": "Fix it", "labels": ["bug"]}]


def test_get_all_open_issues_lightweight_excludes_pull_requests():
    svc = _make_service()
    body = json.dumps(
        [
            {"number": 1, "title": "Issue", "labels": []},
            {
                "number": 2,
                "title": "PR",
                "labels": [],
                "pull_request": {"url": "x"},
            },
        ]
    ).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        result = svc.get_all_open_issues_lightweight()
    assert [r["number"] for r in result] == [1]


def test_get_all_open_issues_lightweight_does_not_filter_recently_closed():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/42",
                data={"state": "closed"},
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&per_page=100",
                payload=[{"number": 42, "title": "T", "labels": []}],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    svc.close_issue(42)
    result = svc.get_all_open_issues_lightweight()

    assert [r["number"] for r in result] == [42]


def test_get_all_open_issues_lightweight_paginates():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&per_page=100",
                payload=[{"number": 1, "title": "A", "labels": []}],
                headers={"Link": '<https://api.github.com/page2>; rel="next"'},
            ),
            _script_step(
                "GET",
                "https://api.github.com/page2",
                payload=[{"number": 2, "title": "B", "labels": [{"name": "feat"}]}],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_all_open_issues_lightweight()

    assert [r["number"] for r in result] == [1, 2]
    assert result[1]["labels"] == ["feat"]


def test_get_all_open_issues_lightweight_paginates_and_normalizes_issue_projections():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/issues?state=open&per_page=100",
                payload=[
                    {
                        "number": 1,
                        "title": None,
                        "labels": [{"name": None}, {"name": "bug"}],
                    },
                    {
                        "number": 2,
                        "title": "PR",
                        "labels": [{"name": "skip-me"}],
                        "pull_request": {"url": "x"},
                    },
                ],
                headers={"Link": '<https://api.github.com/page2>; rel="next"'},
            ),
            _script_step(
                "GET",
                "https://api.github.com/page2",
                payload=[
                    {"number": 3, "title": "Keep me", "labels": None},
                ],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.get_all_open_issues_lightweight()

    assert result == [
        {"number": 1, "title": "", "labels": ["", "bug"]},
        {"number": 3, "title": "Keep me", "labels": []},
    ]


def test_get_all_open_issues_lightweight_normalizes_mixed_open_issue_payload():
    svc = _make_service()
    body = json.dumps(
        [
            "skip",
            {"title": "missing number"},
            {"number": "1", "title": None, "labels": [{"name": "bug"}]},
            {
                "number": 2,
                "title": "PR",
                "labels": [],
                "pull_request": {"url": "x"},
            },
            {"number": "3", "title": "Keep me", "labels": [{"name": "feat"}]},
        ]
    ).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        result = svc.get_all_open_issues_lightweight()

    assert result == [
        {"number": 1, "title": "", "labels": ["bug"]},
        {"number": 3, "title": "Keep me", "labels": ["feat"]},
    ]


# ── close_completed_parent_issues ────────────────────────────────────────────


def test_close_completed_parent_issues_closes_parents_with_all_closed_subs():
    open_list_path = "/repos/owner/repo/issues?state=open&per_page=100"
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                open_list_path,
                payload=[{"number": 10}, {"number": 11}],
                headers={"Link": ""},
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/10/sub_issues",
                payload=[{"number": 1, "state": "closed"}],
                headers={"Link": ""},
            ),
            _script_step(
                "PATCH",
                "/repos/owner/repo/issues/10",
                data={"state": "closed"},
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/11/sub_issues",
                payload=[{"number": 2, "state": "open"}],
                headers={"Link": ""},
            ),
            _script_step(
                "GET",
                open_list_path,
                payload=[{"number": 11}],
                headers={"Link": ""},
            ),
            _script_step(
                "GET",
                "/repos/owner/repo/issues/11/sub_issues",
                payload=[{"number": 2, "state": "open"}],
                headers={"Link": ""},
            ),
        ]
    )
    svc = _make_service(transport=transport)

    svc.close_completed_parent_issues()

    assert transport.requests == [
        _GithubTransportRequest("GET", open_list_path, None),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/10/sub_issues", None),
        _GithubTransportRequest(
            "PATCH", "/repos/owner/repo/issues/10", {"state": "closed"}
        ),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/11/sub_issues", None),
        _GithubTransportRequest("GET", open_list_path, None),
        _GithubTransportRequest("GET", "/repos/owner/repo/issues/11/sub_issues", None),
    ]


# ── list_labels / create_label / delete_label ────────────────────────────────


def test_list_labels_returns_dict_items_from_every_page():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/repos/owner/repo/labels?per_page=100",
                payload=[{"name": "bug"}, "skip-me"],
                headers={
                    "Link": '<https://api.github.com/repos/owner/repo/labels?per_page=100&page=2>; rel="next"'
                },
            ),
            _script_step(
                "GET",
                "https://api.github.com/repos/owner/repo/labels?per_page=100&page=2",
                payload=[{"name": "feat"}, 123],
            ),
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.list_labels()

    assert [label["name"] for label in result] == ["bug", "feat"]
    assert transport.requests == [
        _GithubTransportRequest("GET", "/repos/owner/repo/labels?per_page=100", None),
        _GithubTransportRequest(
            "GET",
            "https://api.github.com/repos/owner/repo/labels?per_page=100&page=2",
            None,
        ),
    ]
    transport.assert_exhausted()


def test_create_label_posts_body_to_labels_endpoint():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "POST",
                "/repos/owner/repo/labels",
                data={"name": "bug", "color": "ff0000"},
                payload={"name": "bug"},
            )
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.create_label({"name": "bug", "color": "ff0000"})

    assert result is None
    assert transport.requests == [
        _GithubTransportRequest(
            "POST",
            "/repos/owner/repo/labels",
            {"name": "bug", "color": "ff0000"},
        )
    ]
    transport.assert_exhausted()


def test_delete_label_sends_delete_with_url_encoded_name():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "DELETE",
                "/repos/owner/repo/labels/needs%20triage",
            )
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.delete_label("needs triage")

    assert result is None
    assert transport.requests == [
        _GithubTransportRequest(
            "DELETE",
            "/repos/owner/repo/labels/needs%20triage",
            None,
        )
    ]
    transport.assert_exhausted()


def test_remove_label_from_issue_returns_none_and_url_encodes_label_name():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "DELETE",
                "/repos/owner/repo/issues/42/labels/team%2Falpha",
            )
        ]
    )
    svc = _make_service(transport=transport)

    result = svc.remove_label_from_issue(42, "team/alpha")

    assert result is None
    assert transport.requests == [
        _GithubTransportRequest(
            "DELETE",
            "/repos/owner/repo/issues/42/labels/team%2Falpha",
            None,
        )
    ]
    transport.assert_exhausted()


def test_remove_label_from_issue_retries_transport_error_and_recovers():
    cause = URLError("dns fail")
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "DELETE",
                "/repos/owner/repo/issues/42/labels/bug",
                error=GithubHttpTransportNetworkError("transport down", cause=cause),
            ),
            _script_step(
                "DELETE",
                "/repos/owner/repo/issues/42/labels/bug",
            ),
        ]
    )
    svc = _make_service(transport=transport)

    with patch("time.sleep") as mock_sleep:
        svc.remove_label_from_issue(42, "bug")

    mock_sleep.assert_called_once_with(10)
    assert transport.requests == [
        _GithubTransportRequest(
            "DELETE",
            "/repos/owner/repo/issues/42/labels/bug",
            None,
        ),
        _GithubTransportRequest(
            "DELETE",
            "/repos/owner/repo/issues/42/labels/bug",
            None,
        ),
    ]
    transport.assert_exhausted()


def test_add_label_to_issue_uses_retry_after_header_when_present():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "POST",
                "/repos/owner/repo/issues/42/labels",
                data={"labels": ["bug"]},
                error=GithubHttpTransportAPIError(
                    "rate limited",
                    status=429,
                    body='{"message":"secondary rate limit"}',
                    method="POST",
                    path="/repos/owner/repo/issues/42/labels",
                    headers={"Retry-After": "9"},
                ),
            ),
            _script_step(
                "POST",
                "/repos/owner/repo/issues/42/labels",
                data={"labels": ["bug"]},
                payload=[],
            ),
        ]
    )
    svc = _make_service(transport=transport)

    with patch("time.sleep") as mock_sleep:
        result = svc.add_label_to_issue(42, "bug")

    assert result is None
    mock_sleep.assert_called_once_with(9)
    assert transport.requests == [
        _GithubTransportRequest(
            "POST",
            "/repos/owner/repo/issues/42/labels",
            {"labels": ["bug"]},
        ),
        _GithubTransportRequest(
            "POST",
            "/repos/owner/repo/issues/42/labels",
            {"labels": ["bug"]},
        ),
    ]
    transport.assert_exhausted()


def test_add_label_to_issue_does_not_retry_stable_403():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "POST",
                "/repos/owner/repo/issues/42/labels",
                data={"labels": ["bug"]},
                error=GithubHttpTransportAPIError(
                    "forbidden",
                    status=403,
                    body='{"message":"Resource not accessible by personal access token"}',
                    method="POST",
                    path="/repos/owner/repo/issues/42/labels",
                ),
            )
        ]
    )
    svc = _make_service(transport=transport)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(GithubAPIError) as exc_info:
            svc.add_label_to_issue(42, "bug")

    assert exc_info.value.status == 403
    mock_sleep.assert_not_called()
    assert transport.requests == [
        _GithubTransportRequest(
            "POST",
            "/repos/owner/repo/issues/42/labels",
            {"labels": ["bug"]},
        )
    ]
    transport.assert_exhausted()


@pytest.mark.parametrize("status", [404, 410])
def test_remove_label_from_issue_treats_gone_as_no_op(status: int):
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "DELETE",
                "/repos/owner/repo/issues/42/labels/bug",
                error=GithubHttpTransportAPIError(
                    "gone",
                    status=status,
                    body='{"message":"Not Found"}',
                    method="DELETE",
                    path="/repos/owner/repo/issues/42/labels/bug",
                ),
            )
        ]
    )
    svc = _make_service(transport=transport)

    svc.remove_label_from_issue(42, "bug")

    assert transport.requests == [
        _GithubTransportRequest(
            "DELETE",
            "/repos/owner/repo/issues/42/labels/bug",
            None,
        )
    ]
    transport.assert_exhausted()


def test_remove_label_from_issue_propagates_non_gone_api_errors():
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "DELETE",
                "/repos/owner/repo/issues/42/labels/bug",
                error=GithubHttpTransportAPIError(
                    "boom",
                    status=403,
                    body="forbidden",
                    method="DELETE",
                    path="/repos/owner/repo/issues/42/labels/bug",
                ),
            )
        ]
    )
    svc = _make_service(transport=transport)

    with pytest.raises(GithubAPIError) as exc_info:
        svc.remove_label_from_issue(42, "bug")

    assert exc_info.value.status == 403
    assert exc_info.value.method == "DELETE"
    assert exc_info.value.path == "/repos/owner/repo/issues/42/labels/bug"
    assert transport.requests == [
        _GithubTransportRequest(
            "DELETE",
            "/repos/owner/repo/issues/42/labels/bug",
            None,
        )
    ]
    transport.assert_exhausted()


# ── create_issue_in ──────────────────────────────────────────────────────────


def test_create_issue_in_posts_to_target_repo_with_payload():
    svc = _make_service()
    body = json.dumps({"number": 42, "html_url": "https://x"}).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ) as m:
        result = svc.create_issue_in(
            "Johannes-Kutsch/pycastle",
            "title",
            "body",
            ["bug", "needs-triage"],
        )
    req = m.call_args[0][0]
    assert req.get_method() == "POST"
    assert req.full_url == (
        "https://api.github.com/repos/Johannes-Kutsch/pycastle/issues"
    )
    assert json.loads(req.data.decode()) == {
        "title": "title",
        "body": "body",
        "labels": ["bug", "needs-triage"],
    }
    assert result == 42


def test_create_issue_in_uses_owner_repo_arg_not_self_repo():
    svc = _make_service(repo="some/other-repo")
    body = json.dumps({"number": 1}).encode()
    with patch(
        "pycastle.services._github_http_transport.urlopen",
        return_value=_make_response(body),
    ) as m:
        svc.create_issue_in("target-owner/target-repo", "t", "b", [])
    req = m.call_args[0][0]
    assert "/repos/target-owner/target-repo/issues" in req.full_url


def test_create_issue_in_does_not_retry_transport_error():
    svc = _make_service()

    with (
        patch(
            "pycastle.services._github_http_transport.urlopen",
            side_effect=URLError("dns fail"),
        ) as mock_urlopen,
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(GithubNetworkError):
            svc.create_issue_in("Johannes-Kutsch/pycastle", "title", "body", ["bug"])

    assert mock_urlopen.call_count == 1
    mock_sleep.assert_not_called()


def test_create_issue_in_with_scripted_transport_preserves_original_network_cause():
    cause = TimeoutError("timed out")
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "POST",
                "/repos/Johannes-Kutsch/pycastle/issues",
                data={"title": "title", "body": "body", "labels": ["bug"]},
                error=GithubHttpTransportNetworkError("transport down", cause=cause),
            )
        ]
    )
    svc = _make_service(transport=transport)

    with pytest.raises(GithubNetworkError) as exc_info:
        svc.create_issue_in("Johannes-Kutsch/pycastle", "title", "body", ["bug"])

    assert exc_info.value.cause is cause
    assert exc_info.value.__cause__ is cause
    assert transport.requests == [
        _GithubTransportRequest(
            "POST",
            "/repos/Johannes-Kutsch/pycastle/issues",
            {"title": "title", "body": "body", "labels": ["bug"]},
        )
    ]
    transport.assert_exhausted()


# ── search_open_issues_by_title ──────────────────────────────────────────────


def test_search_open_issues_by_title_returns_numbers_from_search_api():
    prefix = "[pycastle] git remote unreachable"
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
                payload={
                    "total_count": 2,
                    "items": [{"number": 10}, {"number": 23}],
                },
            )
        ]
    )
    svc = _make_service(transport=transport)
    result = svc.search_open_issues_by_title(prefix)

    assert transport.requests == [
        _GithubTransportRequest(
            "GET",
            "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
            None,
        )
    ]
    transport.assert_exhausted()
    assert result == [10, 23]


def test_search_open_issues_by_title_returns_empty_list_when_no_matches():
    prefix = "[pycastle] git remote unreachable"
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
                payload={"total_count": 0, "items": []},
            )
        ]
    )
    svc = _make_service(transport=transport)
    result = svc.search_open_issues_by_title(prefix)

    assert transport.requests == [
        _GithubTransportRequest(
            "GET",
            "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
            None,
        )
    ]
    transport.assert_exhausted()
    assert result == []


def test_search_open_issues_by_title_returns_empty_list_when_payload_is_not_dict():
    prefix = "[pycastle] git remote unreachable"
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
                payload=[],
            )
        ]
    )
    svc = _make_service(transport=transport)
    result = svc.search_open_issues_by_title(prefix)

    assert transport.requests == [
        _GithubTransportRequest(
            "GET",
            "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
            None,
        )
    ]
    transport.assert_exhausted()
    assert result == []


def test_search_open_issues_by_title_returns_empty_list_when_items_missing():
    prefix = "[pycastle] git remote unreachable"
    transport = _ScriptedGithubTransport(
        [
            _script_step(
                "GET",
                "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
                payload={"total_count": 0},
            )
        ]
    )
    svc = _make_service(transport=transport)
    result = svc.search_open_issues_by_title(prefix)

    assert transport.requests == [
        _GithubTransportRequest(
            "GET",
            "/search/issues?q=%5Bpycastle%5D%20git%20remote%20unreachable%20in%3Atitle%20state%3Aopen%20repo%3Aowner%2Frepo&per_page=100",
            None,
        )
    ]
    transport.assert_exhausted()
    assert result == []


# ── No real network ──────────────────────────────────────────────────────────


def test_no_real_network_call(monkeypatch):
    """Sanity: a misconfigured test must not be able to hit the network."""

    def boom(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr("pycastle.services._github_http_transport.urlopen", boom)
    svc = _make_service()
    with pytest.raises(AssertionError):
        svc.check_auth()
