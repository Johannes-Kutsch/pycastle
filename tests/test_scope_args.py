import pytest

from pycastle.config import Config
from pycastle.prompts.pipeline import PromptRenderError, PromptTemplate, Scope
from pycastle.prompts.renderer import PromptRenderer
from pycastle.prompts.scope_args import (
    build_plan_scope_args,
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


def test_build_issue_scope_args_raises_key_error_when_body_missing():
    with pytest.raises(KeyError):
        build_issue_scope_args(
            {"number": 1, "title": "T", "comments": []}, extra_scope_args={}
        )


def test_build_issue_scope_args_raises_key_error_when_comments_missing():
    with pytest.raises(KeyError):
        build_issue_scope_args(
            {"number": 1, "title": "T", "body": ""}, extra_scope_args={}
        )


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
