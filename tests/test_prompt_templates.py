from pathlib import Path

import pytest

from pycastle.config import Config
from pycastle.iteration.implement import _format_feedback_commands
from pycastle.prompt_pipeline import PromptRenderError, _render

REPO_ROOT = Path(__file__).parent.parent

_cfg = Config()


def assert_template_renders(prompt_file: Path, args: dict[str, str]) -> str:
    """Load a prompt template and render it with args; raises on missing placeholders."""
    template = prompt_file.read_text(encoding="utf-8")
    return _render(template, args)


# ── Cycle 1: Merger template renders with all required args ──────────────────


def test_merger_template_renders_without_error():
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "merge-prompt.md"
    args = {
        "BRANCHES": "- branch-a\n- branch-b",
        "CHECKS": " && ".join(cmd for _, cmd in _cfg.preflight_checks),
    }
    assert_template_renders(prompt_file, args)


# ── Cycle 2: CHECKS value matches live config, no raw placeholder in output ──


def test_merger_checks_arg_matches_preflight_config():
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "merge-prompt.md"
    expected_checks = " && ".join(cmd for _, cmd in _cfg.preflight_checks)
    args = {
        "BRANCHES": "- branch-a",
        "CHECKS": expected_checks,
    }
    rendered = assert_template_renders(prompt_file, args)
    assert "{{CHECKS}}" not in rendered
    assert expected_checks in rendered


def test_merger_template_fails_without_checks_arg():
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "merge-prompt.md"
    args = {"BRANCHES": "- branch-a", "ISSUES": "- #1: Title"}  # CHECKS missing
    with pytest.raises(PromptRenderError, match="CHECKS"):
        assert_template_renders(prompt_file, args)


# ── Cycle 3: Planner template renders without error ───────────────────────────


def test_planner_template_renders_without_error():
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "plan-prompt.md"
    assert_template_renders(prompt_file, {"OPEN_ISSUES_JSON": "[]"})


def test_planner_template_fails_without_open_issues_json_arg():
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "plan-prompt.md"
    with pytest.raises(PromptRenderError, match="OPEN_ISSUES_JSON"):
        assert_template_renders(prompt_file, {})


def test_planner_template_does_not_contain_issue_label_or_shell_expression():
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "plan-prompt.md"
    content = prompt_file.read_text(encoding="utf-8")
    assert "{{ISSUE_LABEL}}" not in content, (
        "plan-prompt.md must not contain {{ISSUE_LABEL}} — use {{OPEN_ISSUES_JSON}}"
    )
    assert "!`" not in content, (
        "plan-prompt.md must not contain inline shell expressions — use {{OPEN_ISSUES_JSON}}"
    )


def test_planner_template_does_not_instruct_branch_emission():
    """Plan prompt must not ask the Planner to emit a branch field — branch is now computed in code."""
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "plan-prompt.md"
    content = prompt_file.read_text(encoding="utf-8")
    assert '"branch"' not in content, (
        "Plan prompt must not instruct the Planner to emit a branch field"
    )


# ── Cycle 4: Implementer template renders without error ───────────────────────


def test_implementer_template_renders_without_error():
    from pycastle.prompt_utils import load_standards

    prompt_file = REPO_ROOT / _cfg.prompts_dir / "implement-prompt.md"
    args = {
        "ISSUE_NUMBER": "42",
        "ISSUE_TITLE": "Fix the thing",
        "BRANCH": "pycastle/issue-42-fix-the-thing",
        "FEEDBACK_COMMANDS": _format_feedback_commands(_cfg.implement_checks),
        **load_standards(REPO_ROOT / _cfg.prompts_dir),
    }
    assert_template_renders(prompt_file, args)


# ── Cycle 4b: Implementer step 0 detects prior run state ─────────────────────


def _get_step0_section(content: str) -> str:
    step0_pos = content.find("### 0.")
    step1_pos = content.find("### 1.")
    assert step0_pos != -1, "implement-prompt.md must contain a ### 0. step"
    assert step1_pos != -1, "implement-prompt.md must contain a ### 1. step"
    assert step0_pos < step1_pos, "step 0 must appear before step 1"
    return content[step0_pos:step1_pos]


@pytest.fixture
def implementer_step0() -> str:
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "implement-prompt.md"
    return _get_step0_section(prompt_file.read_text(encoding="utf-8"))


def test_implementer_step0_runs_git_log_before_any_other_step(implementer_step0: str):
    assert "git log main..HEAD --oneline" in implementer_step0


def test_implementer_step0_emits_complete_promise_when_commits_found(
    implementer_step0: str,
):
    assert "<promise>COMPLETE</promise>" in implementer_step0


def test_implementer_step0_instructs_continue_from_uncommitted_changes(
    implementer_step0: str,
):
    assert "git status" in implementer_step0
    assert "continue" in implementer_step0.lower()


def test_implementer_step0_falls_through_to_step1_when_clean(implementer_step0: str):
    assert (
        "step 1" in implementer_step0.lower()
        or "fall through" in implementer_step0.lower()
    )


# ── Cycle 5: Reviewer template renders without error ─────────────────────────


def test_reviewer_template_renders_without_error():
    from pycastle.prompt_utils import load_standards

    prompt_file = REPO_ROOT / _cfg.prompts_dir / "review-prompt.md"
    args = {
        "ISSUE_NUMBER": "42",
        "ISSUE_TITLE": "Fix the thing",
        "BRANCH": "pycastle/issue-42-fix-the-thing",
        "FEEDBACK_COMMANDS": _format_feedback_commands(_cfg.implement_checks),
        **load_standards(REPO_ROOT / _cfg.prompts_dir),
    }
    assert_template_renders(prompt_file, args)


# ── Cycle 6: Preflight-issue template renders without error ──────────────────


def test_preflight_issue_template_renders_without_error():
    prompt_file = REPO_ROOT / _cfg.prompts_dir / "preflight-issue.md"
    args = {
        "CHECK_NAME": "pytest",
        "COMMAND": "pytest",
        "OUTPUT": "1 failed",
    }
    rendered = assert_template_renders(prompt_file, args)
    assert "{{CHECK_NAME}}" not in rendered
    assert "{{COMMAND}}" not in rendered
    assert "{{OUTPUT}}" not in rendered
