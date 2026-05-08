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


# ── close_issue ──────────────────────────────────────────────────────────────


def test_close_issue_sends_patch_with_state_closed():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(b'{"state":"closed"}'),
    ) as m:
        svc.close_issue(42)
    req = m.call_args[0][0]
    assert req.get_method() == "PATCH"
    assert req.full_url == "https://api.github.com/repos/owner/repo/issues/42"
    assert json.loads(req.data.decode()) == {"state": "closed"}


def test_close_issue_raises_github_api_error_on_failure():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        side_effect=_make_http_error(404, b'{"message":"Not Found"}'),
    ):
        with pytest.raises(GithubAPIError):
            svc.close_issue(42)


# ── get_issue_title / get_labels ─────────────────────────────────────────────


def test_get_issue_title_returns_title():
    svc = _make_service()
    body = json.dumps({"number": 7, "title": "Fix bug"}).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
    ):
        assert svc.get_issue_title(7) == "Fix bug"


def test_get_labels_returns_label_names():
    svc = _make_service()
    body = json.dumps(
        {"labels": [{"name": "bug"}, {"name": "ready-for-agent"}]}
    ).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
    ):
        assert svc.get_labels(7) == ["bug", "ready-for-agent"]


def test_get_labels_returns_empty_list_when_no_labels():
    svc = _make_service()
    body = json.dumps({"labels": []}).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
    ):
        assert svc.get_labels(7) == []


# ── get_parent ───────────────────────────────────────────────────────────────


def test_get_parent_returns_parent_number():
    svc = _make_service()
    body = json.dumps({"parent": {"number": 100}}).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
    ):
        assert svc.get_parent(5) == 100


def test_get_parent_returns_none_when_no_parent():
    svc = _make_service()
    body = json.dumps({"number": 5}).encode()
    with patch(
        "pycastle.services.github_service.urlopen", return_value=_make_response(body)
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
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        assert svc.get_open_sub_issues(10) == [1, 3]


# ── add_sub_issue ────────────────────────────────────────────────────────────


def test_add_sub_issue_posts_to_correct_endpoint():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(b""),
    ) as m:
        svc.add_sub_issue(parent_number=10, child_number=20)
    req = m.call_args[0][0]
    assert "/repos/owner/repo/issues/10/sub_issues" in req.full_url


def test_add_sub_issue_sends_child_as_sub_issue_id():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(b""),
    ) as m:
        svc.add_sub_issue(parent_number=10, child_number=20)
    req = m.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"sub_issue_id": 20}


# ── get_issue_comments ───────────────────────────────────────────────────────


def test_get_issue_comments_returns_author_created_at_and_body():
    svc = _make_service()
    body = json.dumps(
        [
            {
                "user": {"login": "alice"},
                "created_at": "2026-01-01T10:00:00Z",
                "body": "first comment",
            },
            {
                "user": {"login": "bob"},
                "created_at": "2026-01-02T10:00:00Z",
                "body": "second comment",
            },
        ]
    ).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body, headers={}),
    ):
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


def test_get_issue_comments_returns_empty_list_when_no_comments():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(b"[]", headers={}),
    ):
        assert svc.get_issue_comments(7) == []


def test_get_issue_comments_hits_comments_endpoint():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(b"[]", headers={}),
    ) as m:
        svc.get_issue_comments(42)
    req = m.call_args[0][0]
    assert "/repos/owner/repo/issues/42/comments" in req.full_url


# ── close_issue_with_parents ─────────────────────────────────────────────────


def test_close_issue_with_parents_closes_parent_when_no_open_siblings():
    svc = _make_service()
    closed: list[int] = []

    def fake_request(method, path, data=None):
        if method == "PATCH":
            closed.append(int(path.rsplit("/", 1)[-1]))
            return None, {}
        if path.endswith("/issues/5"):
            return {"parent": {"number": 50}}, {}
        if path.endswith("/issues/50"):
            return {"parent": None}, {}
        if "/sub_issues" in path:
            # parent #50's children all closed
            return [{"number": 5, "state": "closed"}], {"Link": ""}
        raise AssertionError(path)

    with patch.object(svc, "_request", side_effect=fake_request):
        svc.close_issue_with_parents(5)
    assert closed == [5, 50]


def test_close_issue_with_parents_stops_when_open_siblings_remain():
    svc = _make_service()
    closed: list[int] = []

    def fake_request(method, path, data=None):
        if method == "PATCH":
            closed.append(int(path.rsplit("/", 1)[-1]))
            return None, {}
        if path.endswith("/issues/5"):
            return {"parent": {"number": 50}}, {}
        if "/sub_issues" in path:
            return [
                {"number": 5, "state": "closed"},
                {"number": 6, "state": "open"},
            ], {"Link": ""}
        raise AssertionError(path)

    with patch.object(svc, "_request", side_effect=fake_request):
        svc.close_issue_with_parents(5)
    assert closed == [5]


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
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        assert svc.get_open_issue_numbers() == [1, 3]


# ── has_open_issues_with_label ───────────────────────────────────────────────


def test_has_open_issues_with_label_true_when_any_match():
    svc = _make_service()
    body = json.dumps([{"number": 1}]).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body),
    ) as m:
        result = svc.has_open_issues_with_label("ready-for-agent")
    assert result is True
    req = m.call_args[0][0]
    assert "labels=ready-for-agent" in req.full_url
    assert "state=open" in req.full_url


def test_has_open_issues_with_label_false_when_empty():
    svc = _make_service()
    body = json.dumps([]).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body),
    ):
        assert svc.has_open_issues_with_label("ready-for-agent") is False


def test_has_open_issues_with_label_excludes_pull_requests():
    svc = _make_service()
    body = json.dumps([{"number": 1, "pull_request": {"url": "x"}}]).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body),
    ):
        assert svc.has_open_issues_with_label("x") is False


# ── get_open_issues ──────────────────────────────────────────────────────────


def test_get_open_issues_returns_number_title_body_labels():
    svc = _make_service()
    body = json.dumps(
        [
            {
                "number": 1,
                "title": "Fix",
                "body": "do it",
                "labels": [{"name": "bug"}],
            }
        ]
    ).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        result = svc.get_open_issues("bug")
    assert result == [
        {
            "number": 1,
            "title": "Fix",
            "body": "do it",
            "labels": ["bug"],
            "comments": [],
        }
    ]


def test_get_open_issues_filters_pull_requests():
    svc = _make_service()
    body = json.dumps(
        [
            {"number": 1, "title": "Issue", "body": "", "labels": []},
            {
                "number": 2,
                "title": "PR",
                "body": "",
                "labels": [],
                "pull_request": {"url": "x"},
            },
        ]
    ).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        result = svc.get_open_issues("bug")
    assert [r["number"] for r in result] == [1]


# ── close_completed_parent_issues ────────────────────────────────────────────


def test_close_completed_parent_issues_closes_parents_with_all_closed_subs():
    svc = _make_service()
    closed: list[int] = []
    open_numbers = [10, 11]

    def fake_request(method, path, data=None):
        if method == "PATCH":
            num = int(path.rsplit("/", 1)[-1])
            closed.append(num)
            open_numbers.remove(num)
            return None, {}
        if "/issues?state=open" in path:
            return [{"number": n} for n in open_numbers], {"Link": ""}
        if path.endswith("/issues/10/sub_issues"):
            return [{"number": 1, "state": "closed"}], {"Link": ""}
        if path.endswith("/issues/11/sub_issues"):
            return [{"number": 2, "state": "open"}], {"Link": ""}
        raise AssertionError(path)

    with patch.object(svc, "_request", side_effect=fake_request):
        svc.close_completed_parent_issues()
    assert closed == [10]


# ── list_labels / create_label / delete_label ────────────────────────────────


def test_list_labels_paginates_and_returns_results():
    svc = _make_service()
    body = json.dumps([{"name": "bug"}, {"name": "feat"}]).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body, headers={}),
    ):
        result = svc.list_labels()
    assert [label["name"] for label in result] == ["bug", "feat"]


def test_create_label_posts_body_to_labels_endpoint():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(b'{"name":"bug"}'),
    ) as m:
        svc.create_label({"name": "bug", "color": "ff0000"})
    req = m.call_args[0][0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://api.github.com/repos/owner/repo/labels"
    assert json.loads(req.data.decode()) == {"name": "bug", "color": "ff0000"}


def test_delete_label_sends_delete_with_url_encoded_name():
    svc = _make_service()
    with patch(
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(b""),
    ) as m:
        svc.delete_label("needs triage")
    req = m.call_args[0][0]
    assert req.get_method() == "DELETE"
    assert req.full_url == (
        "https://api.github.com/repos/owner/repo/labels/needs%20triage"
    )


# ── create_issue_in ──────────────────────────────────────────────────────────


def test_create_issue_in_posts_to_target_repo_with_payload():
    svc = _make_service()
    body = json.dumps({"number": 42, "html_url": "https://x"}).encode()
    with patch(
        "pycastle.services.github_service.urlopen",
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
        "pycastle.services.github_service.urlopen",
        return_value=_make_response(body),
    ) as m:
        svc.create_issue_in("target-owner/target-repo", "t", "b", [])
    req = m.call_args[0][0]
    assert "/repos/target-owner/target-repo/issues" in req.full_url


# ── No real network ──────────────────────────────────────────────────────────


def test_no_real_network_call(monkeypatch):
    """Sanity: a misconfigured test must not be able to hit the network."""

    def boom(*args, **kwargs):
        raise AssertionError("real network call attempted")

    monkeypatch.setattr("pycastle.services.github_service.urlopen", boom)
    svc = _make_service()
    with pytest.raises(AssertionError):
        svc.check_auth()
