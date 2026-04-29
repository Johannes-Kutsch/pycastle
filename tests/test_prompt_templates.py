from pathlib import Path

import pytest

from pycastle.defaults.config import (
    IMPLEMENT_CHECKS,
    PREFLIGHT_CHECKS,
    PROMPTS_DIR,
)
from pycastle.orchestrator import _format_feedback_commands
from pycastle.prompt_pipeline import PromptRenderError, _render

REPO_ROOT = Path(__file__).parent.parent


def assert_template_renders(prompt_file: Path, args: dict[str, str]) -> str:
    """Load a prompt template and render it with args; raises on missing placeholders."""
    template = prompt_file.read_text(encoding="utf-8")
    return _render(template, args)


# ── Cycle 1: Merger template renders with all required args ──────────────────


def test_merger_template_renders_without_error():
    prompt_file = REPO_ROOT / PROMPTS_DIR / "merge-prompt.md"
    args = {
        "BRANCHES": "- branch-a\n- branch-b",
        "ISSUES": "- #1: Title A\n- #2: Title B",
        "CHECKS": " && ".join(cmd for _, cmd in PREFLIGHT_CHECKS),
    }
    assert_template_renders(prompt_file, args)


# ── Cycle 2: CHECKS value matches live config, no raw placeholder in output ──


def test_merger_checks_arg_matches_preflight_config():
    prompt_file = REPO_ROOT / PROMPTS_DIR / "merge-prompt.md"
    expected_checks = " && ".join(cmd for _, cmd in PREFLIGHT_CHECKS)
    args = {
        "BRANCHES": "- branch-a",
        "ISSUES": "- #1: Title",
        "CHECKS": expected_checks,
    }
    rendered = assert_template_renders(prompt_file, args)
    assert "{{CHECKS}}" not in rendered
    assert expected_checks in rendered


def test_merger_template_fails_without_checks_arg():
    prompt_file = REPO_ROOT / PROMPTS_DIR / "merge-prompt.md"
    args = {"BRANCHES": "- branch-a", "ISSUES": "- #1: Title"}  # CHECKS missing
    with pytest.raises(PromptRenderError, match="CHECKS"):
        assert_template_renders(prompt_file, args)


# ── Cycle 3: Planner template renders without error ───────────────────────────


def test_planner_template_renders_without_error():
    prompt_file = REPO_ROOT / PROMPTS_DIR / "plan-prompt.md"
    assert_template_renders(prompt_file, {"OPEN_ISSUES_JSON": "[]"})


def test_planner_template_does_not_instruct_branch_emission():
    """Plan prompt must not ask the Planner to emit a branch field — branch is now computed in code."""
    prompt_file = REPO_ROOT / PROMPTS_DIR / "plan-prompt.md"
    content = prompt_file.read_text(encoding="utf-8")
    assert '"branch"' not in content, (
        "Plan prompt must not instruct the Planner to emit a branch field"
    )


# ── Cycle 4: Implementer template renders without error ───────────────────────


def test_implementer_template_renders_without_error():
    prompt_file = REPO_ROOT / PROMPTS_DIR / "implement-prompt.md"
    args = {
        "ISSUE_NUMBER": "42",
        "ISSUE_TITLE": "Fix the thing",
        "BRANCH": "sandcastle/issue-42-fix-the-thing",
        "FEEDBACK_COMMANDS": _format_feedback_commands(IMPLEMENT_CHECKS),
    }
    assert_template_renders(prompt_file, args)


# ── Cycle 5: Reviewer template renders without error ─────────────────────────


def test_reviewer_template_renders_without_error():
    prompt_file = REPO_ROOT / PROMPTS_DIR / "review-prompt.md"
    args = {
        "ISSUE_NUMBER": "42",
        "ISSUE_TITLE": "Fix the thing",
        "BRANCH": "sandcastle/issue-42-fix-the-thing",
        "FEEDBACK_COMMANDS": _format_feedback_commands(IMPLEMENT_CHECKS),
    }
    assert_template_renders(prompt_file, args)


# ── Cycle 6: Preflight-issue template renders without error ──────────────────


def test_preflight_issue_template_renders_without_error():
    prompt_file = REPO_ROOT / PROMPTS_DIR / "preflight-issue.md"
    args = {
        "CHECK_NAME": "pytest",
        "COMMAND": "pytest",
        "OUTPUT": "1 failed",
    }
    rendered = assert_template_renders(prompt_file, args)
    assert "{{CHECK_NAME}}" not in rendered
    assert "{{COMMAND}}" not in rendered
    assert "{{OUTPUT}}" not in rendered
