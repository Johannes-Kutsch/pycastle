"""Regression guards for the default prompt templates bundled with pycastle."""

import asyncio
from pathlib import Path

import pytest

from pycastle.config import Config
from pycastle.prompt_pipeline import PromptRenderError, PromptRenderer, PromptTemplate

_DEFAULTS = Path(__file__).parent.parent / "src" / "pycastle" / "defaults" / "prompts"
_IMPROVE = _DEFAULTS / "improve"

_cfg = Config(prompts_dir=_DEFAULTS)
_renderer = PromptRenderer(_cfg)


async def _noop_exec(cmd: str) -> str:
    return ""


def _render(template: PromptTemplate, scope_args: dict[str, str]) -> str:
    return asyncio.run(_renderer.render(template, scope_args, _noop_exec))


# ── Merger template ───────────────────────────────────────────────────────────


def test_merger_default_template_renders_without_error():
    _render(PromptTemplate.MERGE, {"BRANCHES": "- branch-a"})


def test_merger_default_template_fails_without_branches_arg():
    with pytest.raises(PromptRenderError, match="BRANCHES"):
        _render(PromptTemplate.MERGE, {})


def test_merger_default_template_expands_checks_placeholder():
    result = _render(PromptTemplate.MERGE, {"BRANCHES": "- branch-a"})
    assert "{{CHECKS}}" not in result


def test_merger_default_template_expands_branches_placeholder():
    result = _render(PromptTemplate.MERGE, {"BRANCHES": "- branch-a"})
    assert "{{BRANCHES}}" not in result
    assert "branch-a" in result


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
    _render(PromptTemplate.PLAN, {"OPEN_ISSUES_JSON": "[]"})


def test_planner_default_template_fails_without_open_issues_json_arg():
    with pytest.raises(PromptRenderError, match="OPEN_ISSUES_JSON"):
        _render(PromptTemplate.PLAN, {})


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


_IMPLEMENT_SCOPE = {
    "ISSUE_NUMBER": "42",
    "ISSUE_TITLE": "Fix the thing",
    "ISSUE_BODY": "do the thing",
    "ISSUE_COMMENTS": "",
    "BRANCH": "pycastle/issue-42",
}


def test_implementer_default_template_renders_without_error():
    _render(PromptTemplate.IMPLEMENT, _IMPLEMENT_SCOPE)


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
    import re

    assert not re.search(r"^### \d+\.", content, re.MULTILINE)
    assert "## 1. Explore" in content


def test_implementer_default_template_renders_issue_body_and_comments():
    rendered = _render(
        PromptTemplate.IMPLEMENT,
        {
            **_IMPLEMENT_SCOPE,
            "ISSUE_BODY": "BODY-CONTENT",
            "ISSUE_COMMENTS": "COMMENTS-CONTENT",
        },
    )
    assert "BODY-CONTENT" in rendered
    assert "COMMENTS-CONTENT" in rendered


# ── Reviewer template ─────────────────────────────────────────────────────────


_REVIEW_SCOPE = {
    "ISSUE_NUMBER": "42",
    "ISSUE_TITLE": "Fix the thing",
    "ISSUE_BODY": "do the thing",
    "ISSUE_COMMENTS": "",
    "BRANCH": "pycastle/issue-42",
}


def test_reviewer_default_template_renders_without_error():
    _render(PromptTemplate.REVIEW, _REVIEW_SCOPE)


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
        PromptTemplate.REVIEW,
        {
            **_REVIEW_SCOPE,
            "ISSUE_BODY": "BODY-CONTENT",
            "ISSUE_COMMENTS": "COMMENTS-CONTENT",
        },
    )
    assert "BODY-CONTENT" in rendered
    assert "COMMENTS-CONTENT" in rendered


# ── Preflight-issue template ──────────────────────────────────────────────────

_PREFLIGHT_SCOPE = {
    "CHECK_NAME": "pytest",
    "COMMAND": "pytest",
    "OUTPUT": "1 failed",
}


def test_preflight_issue_default_template_renders_without_error():
    result = _render(PromptTemplate.PREFLIGHT_ISSUE, _PREFLIGHT_SCOPE)
    assert "{{CHECK_NAME}}" not in result
    assert "{{COMMAND}}" not in result
    assert "{{OUTPUT}}" not in result
    assert "{{BUG_LABEL}}" not in result
    assert "{{ISSUE_LABEL}}" not in result
    assert "{{HITL_LABEL}}" not in result


def test_preflight_issue_template_fails_without_check_name_arg():
    args = {k: v for k, v in _PREFLIGHT_SCOPE.items() if k != "CHECK_NAME"}
    with pytest.raises(PromptRenderError, match="CHECK_NAME"):
        _render(PromptTemplate.PREFLIGHT_ISSUE, args)


def test_preflight_issue_template_fails_without_command_arg():
    args = {k: v for k, v in _PREFLIGHT_SCOPE.items() if k != "COMMAND"}
    with pytest.raises(PromptRenderError, match="COMMAND"):
        _render(PromptTemplate.PREFLIGHT_ISSUE, args)


def test_preflight_issue_template_fails_without_output_arg():
    args = {k: v for k, v in _PREFLIGHT_SCOPE.items() if k != "OUTPUT"}
    with pytest.raises(PromptRenderError, match="OUTPUT"):
        _render(PromptTemplate.PREFLIGHT_ISSUE, args)


def test_preflight_issue_template_expands_label_placeholders():
    # Labels come from Config defaults and are substituted as global args.
    result = _render(PromptTemplate.PREFLIGHT_ISSUE, _PREFLIGHT_SCOPE)
    assert _cfg.bug_label in result
    assert _cfg.issue_label in result
    assert _cfg.hitl_label in result


# ── Phase 2 PRD template ──────────────────────────────────────────────────────

_PRD_SCOPE = {"IMPROVE_SHORT_SID": "abcd1234"}


def test_prd_prompt_renders_without_error():
    _render(PromptTemplate.IMPROVE_PRD, _PRD_SCOPE)


def test_prd_prompt_fails_without_short_sid_arg():
    with pytest.raises(PromptRenderError, match="IMPROVE_SHORT_SID"):
        _render(PromptTemplate.IMPROVE_PRD, {})


def test_prd_prompt_expands_short_sid_in_dedup_search():
    result = _render(PromptTemplate.IMPROVE_PRD, _PRD_SCOPE)
    assert "{{IMPROVE_SHORT_SID}}" not in result
    assert "abcd1234" in result


def test_prd_prompt_instructs_afk_safety_confirmation():
    content = (_IMPROVE / "02-prd.md").read_text(encoding="utf-8")
    assert "afk" in content.lower() or "autonomous" in content.lower()


def test_prd_prompt_instructs_session_footer():
    content = (_IMPROVE / "02-prd.md").read_text(encoding="utf-8")
    assert "_Filed by improve session" in content


# ── Phase 3 sub-issues template ───────────────────────────────────────────────

_ISSUES_SCOPE = {
    "IMPROVE_SHORT_SID": "abcd1234",
    "ISSUE_NUMBER": "99",
    "ISSUE_TITLE": "Parent PRD title",
    "ISSUE_BODY": "Parent PRD body",
    "ISSUE_COMMENTS": "",
}


def test_issues_prompt_renders_without_error():
    _render(PromptTemplate.IMPROVE_ISSUES, _ISSUES_SCOPE)


def test_issues_prompt_fails_without_short_sid_arg():
    incomplete = {k: v for k, v in _ISSUES_SCOPE.items() if k != "IMPROVE_SHORT_SID"}
    with pytest.raises(PromptRenderError):
        _render(PromptTemplate.IMPROVE_ISSUES, incomplete)


def test_issues_prompt_expands_short_sid_in_dedup_search():
    result = _render(PromptTemplate.IMPROVE_ISSUES, _ISSUES_SCOPE)
    assert "{{IMPROVE_SHORT_SID}}" not in result
    assert "abcd1234" in result


def test_issues_prompt_expands_issue_number_in_task_line():
    result = _render(PromptTemplate.IMPROVE_ISSUES, _ISSUES_SCOPE)
    assert "{{ISSUE_NUMBER}}" not in result
    assert "99" in result


def test_issues_prompt_expands_issue_title_and_body():
    result = _render(
        PromptTemplate.IMPROVE_ISSUES,
        {**_ISSUES_SCOPE, "ISSUE_TITLE": "PRD-TITLE", "ISSUE_BODY": "PRD-BODY"},
    )
    assert "PRD-TITLE" in result
    assert "PRD-BODY" in result


def test_issues_prompt_top_level_sections():
    content = (_IMPROVE / "03-issues.md").read_text(encoding="utf-8")
    top_level = [line for line in content.splitlines() if line.startswith("# ")]
    assert "# TASK" in top_level
    assert "# CONTEXT" in top_level


def test_issues_prompt_has_explore_step():
    content = (_IMPROVE / "03-issues.md").read_text(encoding="utf-8")
    assert "## 1. Explore" in content


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

_NO_CANDIDATE_SCOPE = {"IMPROVE_SHORT_SID": "abcd1234"}


def test_no_candidate_prompt_renders_without_error():
    _render(PromptTemplate.IMPROVE_NO_CANDIDATE, _NO_CANDIDATE_SCOPE)


def test_no_candidate_prompt_fails_without_short_sid_arg():
    with pytest.raises(PromptRenderError, match="IMPROVE_SHORT_SID"):
        _render(PromptTemplate.IMPROVE_NO_CANDIDATE, {})


def test_no_candidate_prompt_expands_short_sid():
    result = _render(PromptTemplate.IMPROVE_NO_CANDIDATE, _NO_CANDIDATE_SCOPE)
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
