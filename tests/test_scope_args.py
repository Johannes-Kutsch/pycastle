import pytest
from unittest.mock import MagicMock

from pycastle.config import Config
from pycastle.prompts.pipeline import PromptRenderError, PromptTemplate, Scope
from pycastle.prompts.renderer import PromptRenderer
from pycastle.prompts.scope_args import (
    build_host_check_scope_args,
    build_preflight_scope_args,
    build_improve_scope_args,
    build_plan_scope_args,
    build_per_issue_scope_args,
    build_interrupted_work_clause,
    build_issue_scope_args,
    validated_scope_args_for_scope,
    validated_scope_args_for_template,
)
from pycastle.session import RunKind


@pytest.fixture(autouse=True)
def _project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def prompts_dir(tmp_path):
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "coordination").mkdir(parents=True)
    return prompts_dir


@pytest.fixture
def cfg():
    return Config()


def test_validated_scope_args_for_template_and_scope_returns_input_unchanged():
    template_args = {
        "ALL_OPEN_ISSUES_JSON": "[]",
        "READY_FOR_AGENT_ISSUES_JSON": "[]",
    }
    assert (
        validated_scope_args_for_template(PromptTemplate.PLAN, template_args)
        is template_args
    )

    scope_args = {"BRANCHES": "branch-a\nbranch-b"}
    assert validated_scope_args_for_scope(Scope.MERGE, scope_args) is scope_args


def test_validated_scope_args_reports_template_or_scope_name_on_key_mismatch():
    with pytest.raises(
        PromptRenderError,
        match=r"template PLAN",
    ) as template_error:
        validated_scope_args_for_template(
            PromptTemplate.PLAN, {"ALL_OPEN_ISSUES_JSON": "[]"}
        )

    assert "missing" in str(template_error.value)
    assert "READY_FOR_AGENT_ISSUES_JSON" in str(template_error.value)

    with pytest.raises(PromptRenderError, match=r"scope MERGE") as scope_error:
        validated_scope_args_for_scope(Scope.MERGE, {"BRANCH": "topic"})

    assert "extra" in str(scope_error.value) or "missing" in str(scope_error.value)


def test_build_issue_scope_args_merges_extra_into_required_keys():
    issue = {
        "number": 1,
        "title": "Fix bug",
        "body": "details",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    result = build_issue_scope_args(
        issue, extra_scope_args={"BRANCH": "pycastle/issue-1"}
    )
    assert set(result.keys()) == {
        "ISSUE_NUMBER",
        "ISSUE_TITLE",
        "ISSUE_BODY",
        "ISSUE_COMMENTS",
        "BRANCH",
    }
    assert result["BRANCH"] == "pycastle/issue-1"


def test_build_issue_scope_args_formats_number_as_string():
    issue = {
        "number": 42,
        "title": "T",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    result = build_issue_scope_args(issue, extra_scope_args={})
    assert result["ISSUE_NUMBER"] == "42"


def test_build_issue_scope_args_uses_empty_string_when_body_missing():
    result = build_issue_scope_args(
        {"number": 1, "title": "T", "comments": []}, extra_scope_args={}
    )

    assert result["ISSUE_BODY"] == ""


def test_build_issue_scope_args_uses_empty_string_when_comments_missing():
    result = build_issue_scope_args(
        {"number": 1, "title": "T", "body": ""}, extra_scope_args={}
    )

    assert result["ISSUE_COMMENTS"] == ""


def test_build_issue_scope_args_formats_comments():
    issue = {
        "number": 1,
        "title": "T",
        "body": "",
        "comments": [
            {"author": "alice", "created_at": "2026-01-01T10:00:00Z", "body": "hi"}
        ],
    }
    result = build_issue_scope_args(issue, extra_scope_args={})
    assert "alice" in result["ISSUE_COMMENTS"]
    assert "2026-01-01T10:00:00Z" in result["ISSUE_COMMENTS"]
    assert "hi" in result["ISSUE_COMMENTS"]


def test_build_issue_scope_args_formats_comment_fallback_values():
    issue = {
        "number": 1,
        "title": "T",
        "body": "",
        "comments": [{}],
    }
    result = build_issue_scope_args(issue, extra_scope_args={})
    assert result["ISSUE_COMMENTS"] == "## Comment by @unknown at unknown time\n\n"


@pytest.mark.parametrize(
    "colliding_key",
    ["ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_BODY", "ISSUE_COMMENTS"],
)
def test_build_issue_scope_args_rejects_collision_with_reserved_keys(colliding_key):
    issue = {
        "number": 1,
        "title": "T",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    with pytest.raises(PromptRenderError):
        build_issue_scope_args(issue, extra_scope_args={colliding_key: "x"})


def test_build_issue_scope_args_raises_on_missing_required_keys():
    with pytest.raises(KeyError):
        build_issue_scope_args({"title": "T"}, extra_scope_args={})
    with pytest.raises(KeyError):
        build_issue_scope_args({"number": 1}, extra_scope_args={})


def test_build_per_issue_scope_args_builds_exact_renderable_per_issue_args():
    issue = {
        "number": 7,
        "title": "Fix prompt args",
        "body": "Detailed issue body",
        "comments": [],
    }

    result = build_per_issue_scope_args(
        issue,
        branch="pycastle/issue-7",
        run_kind=RunKind.FRESH,
        is_dirty=True,
    )

    assert validated_scope_args_for_scope(Scope.PER_ISSUE, result) is result
    assert result == {
        "ISSUE_NUMBER": "7",
        "ISSUE_TITLE": "Fix prompt args",
        "ISSUE_BODY": "Detailed issue body",
        "ISSUE_COMMENTS": "",
        "BRANCH": "pycastle/issue-7",
        "INTERRUPTED_WORK": build_interrupted_work_clause(RunKind.FRESH, is_dirty=True),
    }


def test_build_issue_scope_args_normalizes_missing_body_and_comments_to_empty():
    result = build_issue_scope_args(
        {"number": 9, "title": "Missing fields"},
        extra_scope_args={},
    )

    assert result["ISSUE_BODY"] == ""
    assert result["ISSUE_COMMENTS"] == ""


def test_build_issue_scope_args_normalizes_none_body_and_comments_to_empty():
    result = build_issue_scope_args(
        {"number": 10, "title": "None fields", "body": None, "comments": None},
        extra_scope_args={},
    )

    assert result["ISSUE_BODY"] == ""
    assert result["ISSUE_COMMENTS"] == ""


@pytest.mark.parametrize(
    ("run_kind", "is_dirty", "expected"),
    [
        (RunKind.FRESH, True, True),
        (RunKind.FRESH, False, False),
        (RunKind.RESUME, True, False),
        (RunKind.RESUME, False, False),
    ],
)
def test_build_interrupted_work_clause_matrix(run_kind, is_dirty, expected):
    result = build_interrupted_work_clause(run_kind, is_dirty=is_dirty)
    assert ("Interrupted Work" in result) is expected


def test_build_plan_scope_args_builds_renderable_plan_args(cfg, prompts_dir):
    import asyncio
    import json

    all_open_issues = [
        {"number": 1, "title": "Open A", "labels": ["ready-for-agent"]},
        {"number": 2, "title": "Open B", "labels": ["blocked"]},
    ]
    ready_for_agent_issues = [
        {
            "number": 1,
            "title": "Open A",
            "body": "x" * 100,
            "comments": [{"author": "alice", "created_at": "2026-01-01", "body": "ok"}],
            "labels": ["ready-for-agent", "behavior-slice"],
        }
    ]
    (prompts_dir / "coordination/plan.md").write_text(
        "All: {{ALL_OPEN_ISSUES_JSON}}\nReady: {{READY_FOR_AGENT_ISSUES_JSON}}"
    )

    scope_args = build_plan_scope_args(
        all_open_issues=all_open_issues,
        ready_for_agent_issues=ready_for_agent_issues,
    )

    assert (
        validated_scope_args_for_template(PromptTemplate.PLAN, scope_args) is scope_args
    )
    assert scope_args["ALL_OPEN_ISSUES_JSON"] == json.dumps(all_open_issues)
    assert scope_args["READY_FOR_AGENT_ISSUES_JSON"] == json.dumps(
        ready_for_agent_issues
    )

    renderer = PromptRenderer(cfg)
    rendered = asyncio.run(
        renderer.render(
            PromptTemplate.PLAN,
            scope_args,
            lambda command: (_ for _ in ()).throw(AssertionError(command)),
        )
    )

    assert rendered == (
        f"All: {json.dumps(all_open_issues)}\n"
        f"Ready: {json.dumps(ready_for_agent_issues)}"
    )


def test_build_plan_scope_args_accepts_empty_issue_lists():
    scope_args = build_plan_scope_args(
        all_open_issues=[],
        ready_for_agent_issues=[],
    )

    assert (
        validated_scope_args_for_template(PromptTemplate.PLAN, scope_args) is scope_args
    )
    assert scope_args == {
        "ALL_OPEN_ISSUES_JSON": "[]",
        "READY_FOR_AGENT_ISSUES_JSON": "[]",
    }


def test_build_preflight_scope_args_builds_exact_renderable_preflight_args(
    cfg, prompts_dir
):
    import asyncio

    (prompts_dir / "diagnostics").mkdir(parents=True)
    (prompts_dir / "diagnostics/preflight-issue.md").write_text(
        "Check: {{CHECK_NAME}}\nCommand: {{COMMAND}}\nOutput: {{OUTPUT}}"
    )

    scope_args = build_preflight_scope_args(
        check_name="[PREFLIGHT] ruff",
        command="ruff check --fix",
        output="E501 line too long",
    )

    assert (
        validated_scope_args_for_template(PromptTemplate.PREFLIGHT_ISSUE, scope_args)
        is scope_args
    )
    assert scope_args == {
        "CHECK_NAME": "[PREFLIGHT] ruff",
        "COMMAND": "ruff check --fix",
        "OUTPUT": "E501 line too long",
    }

    renderer = PromptRenderer(cfg)
    rendered = asyncio.run(
        renderer.render(
            PromptTemplate.PREFLIGHT_ISSUE,
            scope_args,
            lambda command: (_ for _ in ()).throw(AssertionError(command)),
        )
    )

    assert rendered == (
        "Check: [PREFLIGHT] ruff\nCommand: ruff check --fix\nOutput: E501 line too long"
    )


def test_build_host_check_scope_args_builds_exact_renderable_host_check_args(
    cfg, prompts_dir
):
    import asyncio
    import platform

    (prompts_dir / "diagnostics").mkdir(parents=True)
    (prompts_dir / "diagnostics/host-check-issue.md").write_text(
        "OS: {{HOST_OS}}\n"
        "Platform: {{HOST_PLATFORM}}\n"
        "SHA: {{CHECKED_SHA}}\n"
        "Check: {{CHECK_NAME}}\n"
        "Command: {{COMMAND}}\n"
        "Output: {{OUTPUT}}"
    )

    scope_args = build_host_check_scope_args(
        checked_sha="abc123def456",
        check_name="pytest-host",
        command="pytest tests/host",
        output="assertion failed",
    )

    assert (
        validated_scope_args_for_template(PromptTemplate.HOST_CHECK_ISSUE, scope_args)
        is scope_args
    )
    assert scope_args == {
        "HOST_OS": platform.system(),
        "HOST_PLATFORM": platform.platform(),
        "CHECKED_SHA": "abc123def456",
        "CHECK_NAME": "pytest-host",
        "COMMAND": "pytest tests/host",
        "OUTPUT": "assertion failed",
    }

    renderer = PromptRenderer(cfg)
    rendered = asyncio.run(
        renderer.render(
            PromptTemplate.HOST_CHECK_ISSUE,
            scope_args,
            lambda command: (_ for _ in ()).throw(AssertionError(command)),
        )
    )

    assert rendered == (
        f"OS: {platform.system()}\n"
        f"Platform: {platform.platform()}\n"
        "SHA: abc123def456\n"
        "Check: pytest-host\n"
        "Command: pytest tests/host\n"
        "Output: assertion failed"
    )


def test_build_improve_scope_args_builds_exact_args_for_each_improve_prompt():
    github_svc = MagicMock()
    github_svc.get_recent_improve_prds.return_value = [
        {"number": 12, "state": "OPEN", "title": "First candidate"}
    ]
    github_svc.get_issue.return_value = {
        "number": 42,
        "title": "Improve PRD",
        "body": "PRD body",
    }
    github_svc.get_issue_comments.return_value = [
        {"author": "alice", "created_at": "2026-01-01T00:00:00Z", "body": "lgtm"}
    ]

    scan_args = build_improve_scope_args(
        PromptTemplate.IMPROVE_SCAN,
        github_svc=github_svc,
        short_sid="abcd1234",
    )
    prd_args = build_improve_scope_args(
        PromptTemplate.IMPROVE_PRD,
        github_svc=github_svc,
        short_sid="abcd1234",
    )
    no_candidate_args = build_improve_scope_args(
        PromptTemplate.IMPROVE_NO_CANDIDATE,
        github_svc=github_svc,
        short_sid="abcd1234",
    )
    issues_args = build_improve_scope_args(
        PromptTemplate.IMPROVE_ISSUES,
        github_svc=github_svc,
        short_sid="abcd1234",
        prd_number=42,
    )

    assert (
        validated_scope_args_for_template(PromptTemplate.IMPROVE_SCAN, scan_args)
        is scan_args
    )
    assert scan_args == {"RECENT_IMPROVE_PRD_TITLES": "#12 OPEN - First candidate"}

    assert (
        validated_scope_args_for_template(PromptTemplate.IMPROVE_PRD, prd_args)
        is prd_args
    )
    assert prd_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_PRDS": "#12 OPEN - First candidate",
    }

    assert (
        validated_scope_args_for_template(
            PromptTemplate.IMPROVE_NO_CANDIDATE, no_candidate_args
        )
        is no_candidate_args
    )
    assert no_candidate_args == prd_args

    assert (
        validated_scope_args_for_template(PromptTemplate.IMPROVE_ISSUES, issues_args)
        is issues_args
    )
    assert issues_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "ISSUE_NUMBER": "42",
        "ISSUE_TITLE": "Improve PRD",
        "ISSUE_BODY": "PRD body",
        "ISSUE_COMMENTS": "## Comment by @alice at 2026-01-01T00:00:00Z\n\nlgtm",
    }


def test_build_improve_scope_args_uses_empty_issue_fields_without_prd_number():
    github_svc = MagicMock()

    issues_args = build_improve_scope_args(
        PromptTemplate.IMPROVE_ISSUES,
        github_svc=github_svc,
        short_sid="abcd1234",
        prd_number=None,
    )

    assert (
        validated_scope_args_for_template(PromptTemplate.IMPROVE_ISSUES, issues_args)
        is issues_args
    )
    assert issues_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "ISSUE_NUMBER": "",
        "ISSUE_TITLE": "",
        "ISSUE_BODY": "",
        "ISSUE_COMMENTS": "",
    }
    github_svc.get_issue.assert_not_called()
    github_svc.get_issue_comments.assert_not_called()


def test_build_improve_scope_args_does_not_lookup_recent_prds_for_issues_prompt():
    github_svc = MagicMock()
    github_svc.get_recent_improve_prds.side_effect = AssertionError(
        "recent PRD lookup is not part of IMPROVE_ISSUES scope construction"
    )
    github_svc.get_issue.return_value = {
        "number": 42,
        "title": "Improve PRD",
        "body": "PRD body",
    }
    github_svc.get_issue_comments.return_value = []

    issues_args = build_improve_scope_args(
        PromptTemplate.IMPROVE_ISSUES,
        github_svc=github_svc,
        short_sid="abcd1234",
        prd_number=42,
    )

    assert (
        validated_scope_args_for_template(PromptTemplate.IMPROVE_ISSUES, issues_args)
        is issues_args
    )
    assert issues_args["ISSUE_NUMBER"] == "42"
    github_svc.get_recent_improve_prds.assert_not_called()
