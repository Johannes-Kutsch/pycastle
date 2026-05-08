"""Regression guards for the default prompt templates bundled with pycastle."""

import asyncio
from pathlib import Path

import pytest

from pycastle.prompt_pipeline import PromptRenderError, load_standards, prepare_prompt

_DEFAULTS = Path(__file__).parent.parent / "src" / "pycastle" / "defaults" / "prompts"
_IMPROVE = _DEFAULTS / "improve"


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


_IMPLEMENT_ARGS = {
    "ISSUE_NUMBER": "42",
    "ISSUE_TITLE": "Fix the thing",
    "ISSUE_BODY": "do the thing",
    "ISSUE_COMMENTS": "",
    "BRANCH": "pycastle/issue-42",
    "FEEDBACK_COMMANDS": "`pytest`",
}


def test_implementer_default_template_renders_without_error():
    _render(
        _DEFAULTS / "implement-prompt.md",
        {**_IMPLEMENT_ARGS, **_standards()},
    )


def test_implementer_default_template_has_no_shell_expression():
    content = (_DEFAULTS / "implement-prompt.md").read_text(encoding="utf-8")
    assert "!`" not in content


def test_implementer_default_template_does_not_invoke_gh():
    content = (_DEFAULTS / "implement-prompt.md").read_text(encoding="utf-8")
    assert "gh issue view" not in content
    assert "gh issue comment" not in content


def test_implementer_default_template_top_level_sections():
    content = (_DEFAULTS / "implement-prompt.md").read_text(encoding="utf-8")
    top_level = [line for line in content.splitlines() if line.startswith("# ")]
    assert top_level == ["# TASK", "# CONTEXT", "# WORKFLOW"]


def test_implementer_default_template_workflow_steps_use_double_hash_numbered():
    content = (_DEFAULTS / "implement-prompt.md").read_text(encoding="utf-8")
    # No legacy ### N. headings remain
    import re

    assert not re.search(r"^### \d+\.", content, re.MULTILINE)
    # Workflow starts at "## 1. Explore"
    assert "## 1. Explore" in content


def test_implementer_default_template_renders_issue_body_and_comments():
    rendered = _render(
        _DEFAULTS / "implement-prompt.md",
        {
            **_IMPLEMENT_ARGS,
            "ISSUE_BODY": "BODY-CONTENT",
            "ISSUE_COMMENTS": "COMMENTS-CONTENT",
            **_standards(),
        },
    )
    assert "BODY-CONTENT" in rendered
    assert "COMMENTS-CONTENT" in rendered


# ── Reviewer template ─────────────────────────────────────────────────────────


_REVIEW_ARGS = {
    "ISSUE_NUMBER": "42",
    "ISSUE_TITLE": "Fix the thing",
    "ISSUE_BODY": "do the thing",
    "ISSUE_COMMENTS": "",
    "BRANCH": "pycastle/issue-42",
    "FEEDBACK_COMMANDS": "`pytest`",
}


def test_reviewer_default_template_renders_without_error():
    _render(
        _DEFAULTS / "review-prompt.md",
        {**_REVIEW_ARGS, **_standards()},
    )


def test_reviewer_default_template_has_no_shell_expression():
    content = (_DEFAULTS / "review-prompt.md").read_text(encoding="utf-8")
    assert "!`" not in content


def test_reviewer_default_template_does_not_invoke_gh():
    content = (_DEFAULTS / "review-prompt.md").read_text(encoding="utf-8")
    assert "gh issue view" not in content
    assert "gh issue comment" not in content


def test_reviewer_default_template_has_no_diff_placeholder():
    content = (_DEFAULTS / "review-prompt.md").read_text(encoding="utf-8")
    assert "{{DIFF}}" not in content, (
        "review-prompt.md must not interpolate {{DIFF}} — reviewer runs git diff itself"
    )


def test_reviewer_default_template_renders_issue_body_and_comments():
    rendered = _render(
        _DEFAULTS / "review-prompt.md",
        {
            **_REVIEW_ARGS,
            "ISSUE_BODY": "BODY-CONTENT",
            "ISSUE_COMMENTS": "COMMENTS-CONTENT",
            **_standards(),
        },
    )
    assert "BODY-CONTENT" in rendered
    assert "COMMENTS-CONTENT" in rendered


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


# ── Phase 2 PRD template ──────────────────────────────────────────────────────

_PRD_ARGS = {"IMPROVE_SHORT_SID": "abcd1234"}


def test_prd_prompt_renders_without_error():
    _render(_IMPROVE / "02-prd.md", _PRD_ARGS)


def test_prd_prompt_fails_without_short_sid_arg():
    with pytest.raises(PromptRenderError, match="IMPROVE_SHORT_SID"):
        _render(_IMPROVE / "02-prd.md", {})


def test_prd_prompt_expands_short_sid_in_dedup_search():
    result = _render(_IMPROVE / "02-prd.md", _PRD_ARGS)
    assert "{{IMPROVE_SHORT_SID}}" not in result
    assert "abcd1234" in result


def test_prd_prompt_instructs_afk_safety_confirmation():
    content = (_IMPROVE / "02-prd.md").read_text(encoding="utf-8")
    assert "afk" in content.lower() or "autonomous" in content.lower()


def test_prd_prompt_instructs_session_footer():
    content = (_IMPROVE / "02-prd.md").read_text(encoding="utf-8")
    assert "_Filed by improve session" in content


# ── Phase 3 sub-issues template ───────────────────────────────────────────────

_ISSUES_ARGS = {"IMPROVE_SHORT_SID": "abcd1234"}


def test_issues_prompt_renders_without_error():
    _render(_IMPROVE / "03-issues.md", _ISSUES_ARGS)


def test_issues_prompt_fails_without_short_sid_arg():
    with pytest.raises(PromptRenderError, match="IMPROVE_SHORT_SID"):
        _render(_IMPROVE / "03-issues.md", {})


def test_issues_prompt_expands_short_sid_in_dedup_search():
    result = _render(_IMPROVE / "03-issues.md", _ISSUES_ARGS)
    assert "{{IMPROVE_SHORT_SID}}" not in result
    assert "abcd1234" in result


def test_issues_prompt_instructs_dedup_check():
    content = (_IMPROVE / "03-issues.md").read_text(encoding="utf-8")
    assert "gh issue list" in content


def test_issues_prompt_instructs_afk_safety_confirmation():
    content = (_IMPROVE / "03-issues.md").read_text(encoding="utf-8")
    assert "afk" in content.lower() or "autonomous" in content.lower()


def test_issues_prompt_instructs_session_footer():
    content = (_IMPROVE / "03-issues.md").read_text(encoding="utf-8")
    assert "_Filed by improve session" in content


def test_issues_prompt_instructs_sub_issue_registration():
    content = (_IMPROVE / "03-issues.md").read_text(encoding="utf-8")
    assert "sub_issues" in content or "sub-issue" in content.lower()


# ── Phase 4 no-candidate report template ─────────────────────────────────────

_NO_CANDIDATE_ARGS = {"IMPROVE_SHORT_SID": "abcd1234"}


def test_no_candidate_prompt_renders_without_error():
    _render(_IMPROVE / "04-no-candidate-report.md", _NO_CANDIDATE_ARGS)


def test_no_candidate_prompt_fails_without_short_sid_arg():
    with pytest.raises(PromptRenderError, match="IMPROVE_SHORT_SID"):
        _render(_IMPROVE / "04-no-candidate-report.md", {})


def test_no_candidate_prompt_expands_short_sid():
    result = _render(_IMPROVE / "04-no-candidate-report.md", _NO_CANDIDATE_ARGS)
    assert "{{IMPROVE_SHORT_SID}}" not in result
    assert "abcd1234" in result


def test_no_candidate_prompt_instructs_dedup_check():
    content = (_IMPROVE / "04-no-candidate-report.md").read_text(encoding="utf-8")
    assert "gh issue list" in content


def test_no_candidate_prompt_instructs_afk_safety_constraints():
    content = (_IMPROVE / "04-no-candidate-report.md").read_text(encoding="utf-8")
    assert "afk" in content.lower() or "autonomous" in content.lower()


def test_no_candidate_prompt_instructs_session_footer():
    content = (_IMPROVE / "04-no-candidate-report.md").read_text(encoding="utf-8")
    assert "_Filed by improve session" in content
