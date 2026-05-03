"""Regression guards for the default prompt templates bundled with pycastle."""

import asyncio
from pathlib import Path

import pytest

from pycastle.prompt_pipeline import PromptRenderError, load_standards, prepare_prompt

_DEFAULTS = Path(__file__).parent.parent / "src" / "pycastle" / "defaults" / "prompts"


async def _noop_exec(cmd: str) -> str:
    return ""


def _render(path: Path, args: dict[str, str]) -> str:
    return asyncio.run(prepare_prompt(path, args, _noop_exec))


def _standards() -> dict[str, str]:
    return load_standards(_DEFAULTS)


# ── Merger template ───────────────────────────────────────────────────────────


def test_merger_default_template_renders_without_error():
    _render(
        _DEFAULTS / "merge-prompt.md", {"BRANCHES": "- branch-a", "CHECKS": "pytest"}
    )


def test_merger_default_template_fails_without_checks_arg():
    with pytest.raises(PromptRenderError, match="CHECKS"):
        _render(_DEFAULTS / "merge-prompt.md", {"BRANCHES": "- branch-a"})


def test_merger_default_template_expands_checks_placeholder():
    result = _render(
        _DEFAULTS / "merge-prompt.md",
        {"BRANCHES": "- branch-a", "CHECKS": "ruff check . && pytest"},
    )
    assert "{{CHECKS}}" not in result
    assert "ruff check . && pytest" in result


def test_merge_prompt_has_no_close_issues_section():
    content = (_DEFAULTS / "merge-prompt.md").read_text(encoding="utf-8")
    assert "CLOSE ISSUES" not in content, (
        "merge-prompt.md must not contain a CLOSE ISSUES section"
    )


def test_merge_prompt_has_no_issues_placeholder():
    content = (_DEFAULTS / "merge-prompt.md").read_text(encoding="utf-8")
    assert "{{ISSUES}}" not in content, (
        "merge-prompt.md must not contain the {{ISSUES}} placeholder"
    )


# ── Planner template ──────────────────────────────────────────────────────────


def test_planner_default_template_renders_without_error():
    _render(_DEFAULTS / "plan-prompt.md", {"OPEN_ISSUES_JSON": "[]"})


def test_planner_default_template_fails_without_open_issues_json_arg():
    with pytest.raises(PromptRenderError, match="OPEN_ISSUES_JSON"):
        _render(_DEFAULTS / "plan-prompt.md", {})


def test_plan_prompt_has_no_issue_label_placeholder():
    content = (_DEFAULTS / "plan-prompt.md").read_text(encoding="utf-8")
    assert "{{ISSUE_LABEL}}" not in content, (
        "plan-prompt.md must not contain {{ISSUE_LABEL}} — use {{OPEN_ISSUES_JSON}}"
    )


def test_plan_prompt_has_no_shell_expression():
    content = (_DEFAULTS / "plan-prompt.md").read_text(encoding="utf-8")
    assert "!`" not in content, (
        "plan-prompt.md must not contain inline shell expressions — use {{OPEN_ISSUES_JSON}}"
    )


def test_plan_prompt_has_no_branch_field_in_output():
    content = (_DEFAULTS / "plan-prompt.md").read_text(encoding="utf-8")
    assert '"branch"' not in content, (
        "plan-prompt.md must not instruct the Planner to emit a branch field"
    )


# ── Implementer template ──────────────────────────────────────────────────────


def test_implementer_default_template_renders_without_error():
    _render(
        _DEFAULTS / "implement-prompt.md",
        {
            "ISSUE_NUMBER": "42",
            "ISSUE_TITLE": "Fix the thing",
            "BRANCH": "pycastle/issue-42",
            "FEEDBACK_COMMANDS": "`pytest`",
            **_standards(),
        },
    )


# ── Reviewer template ─────────────────────────────────────────────────────────


def test_reviewer_default_template_renders_without_error():
    _render(
        _DEFAULTS / "review-prompt.md",
        {
            "ISSUE_NUMBER": "42",
            "ISSUE_TITLE": "Fix the thing",
            "BRANCH": "pycastle/issue-42",
            "FEEDBACK_COMMANDS": "`pytest`",
            **_standards(),
        },
    )


# ── Preflight-issue template ──────────────────────────────────────────────────

_PREFLIGHT_ARGS = {
    "CHECK_NAME": "pytest",
    "COMMAND": "pytest",
    "OUTPUT": "1 failed",
    "BUG_LABEL": "bug",
    "ISSUE_LABEL": "ready-for-agent",
    "HITL_LABEL": "ready-for-human",
}


def test_preflight_issue_default_template_renders_without_error():
    result = _render(_DEFAULTS / "preflight-issue.md", _PREFLIGHT_ARGS)
    assert "{{CHECK_NAME}}" not in result
    assert "{{COMMAND}}" not in result
    assert "{{OUTPUT}}" not in result
    assert "{{BUG_LABEL}}" not in result
    assert "{{ISSUE_LABEL}}" not in result
    assert "{{HITL_LABEL}}" not in result


def test_preflight_issue_template_fails_without_bug_label_arg():
    args = {k: v for k, v in _PREFLIGHT_ARGS.items() if k != "BUG_LABEL"}
    with pytest.raises(PromptRenderError, match="BUG_LABEL"):
        _render(_DEFAULTS / "preflight-issue.md", args)


def test_preflight_issue_template_fails_without_issue_label_arg():
    args = {k: v for k, v in _PREFLIGHT_ARGS.items() if k != "ISSUE_LABEL"}
    with pytest.raises(PromptRenderError, match="ISSUE_LABEL"):
        _render(_DEFAULTS / "preflight-issue.md", args)


def test_preflight_issue_template_fails_without_hitl_label_arg():
    args = {k: v for k, v in _PREFLIGHT_ARGS.items() if k != "HITL_LABEL"}
    with pytest.raises(PromptRenderError, match="HITL_LABEL"):
        _render(_DEFAULTS / "preflight-issue.md", args)


def test_preflight_issue_template_expands_label_placeholders():
    result = _render(
        _DEFAULTS / "preflight-issue.md",
        {
            **_PREFLIGHT_ARGS,
            "BUG_LABEL": "custom-bug",
            "ISSUE_LABEL": "custom-agent",
            "HITL_LABEL": "custom-human",
        },
    )
    assert "custom-bug" in result
    assert "custom-agent" in result
    assert "custom-human" in result
