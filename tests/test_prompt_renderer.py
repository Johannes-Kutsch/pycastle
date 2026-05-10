import asyncio
from pathlib import Path

import pytest

from pycastle.config import Config
from pycastle.prompt_pipeline import (
    PromptRenderError,
    PromptRenderer,
    PromptTemplate,
    Scope,
)


async def _noop_exec(cmd: str) -> str:
    return f"output-of:{cmd}"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "improve").mkdir()
    (tmp_path / "coding-standards").mkdir()
    return tmp_path


@pytest.fixture
def cfg(prompts_dir: Path) -> Config:
    return Config(prompts_dir=prompts_dir)


# ── Tracer bullet: renderer renders a global placeholder ──────────────────────


def test_renderer_renders_global_placeholder(cfg, prompts_dir):
    (prompts_dir / "implement-prompt.md").write_text("Label: {{READY_FOR_AGENT_LABEL}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "title",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "pycastle/issue-1",
            },
            _noop_exec,
        )
    )

    assert result == "Label: ready-for-agent"


# ── Scope enum has correct placeholder sets ───────────────────────────────────


def test_scope_per_issue_placeholders():
    assert Scope.PER_ISSUE.placeholders == frozenset(
        {"ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_BODY", "ISSUE_COMMENTS", "BRANCH"}
    )


def test_scope_merge_placeholders():
    assert Scope.MERGE.placeholders == frozenset({"BRANCHES"})


def test_scope_plan_placeholders():
    assert Scope.PLAN.placeholders == frozenset(
        {"ALL_OPEN_ISSUES_JSON", "READY_FOR_AGENT_ISSUES_JSON"}
    )


def test_scope_preflight_placeholders():
    assert Scope.PREFLIGHT.placeholders == frozenset(
        {"CHECK_NAME", "COMMAND", "OUTPUT"}
    )


def test_scope_improve_scan_is_empty():
    assert Scope.IMPROVE_SCAN.placeholders == frozenset()


def test_scope_improve_session_placeholders():
    assert Scope.IMPROVE_SESSION.placeholders == frozenset({"IMPROVE_SHORT_SID"})


def test_scope_resume_is_empty():
    assert Scope.RESUME.placeholders == frozenset()


def test_scope_improve_issues_placeholders():
    assert Scope.IMPROVE_ISSUES.placeholders == frozenset(
        {
            "IMPROVE_SHORT_SID",
            "ISSUE_NUMBER",
            "ISSUE_TITLE",
            "ISSUE_BODY",
            "ISSUE_COMMENTS",
        }
    )


def test_scopes_are_distinct_members():
    # Regression: empty-frozenset values were aliased by Enum, collapsing
    # IMPROVE_SCAN and RESUME into a single member.
    assert Scope.IMPROVE_SCAN is not Scope.RESUME
    assert len(list(Scope)) == 9


# ── PromptTemplate enum has correct filename and scope ────────────────────────


def test_template_implement_has_correct_filename_and_scope():
    assert PromptTemplate.IMPLEMENT.filename == "implement-prompt.md"
    assert PromptTemplate.IMPLEMENT.scope == Scope.PER_ISSUE


def test_template_review_has_per_issue_scope():
    assert PromptTemplate.REVIEW.filename == "review-prompt.md"
    assert PromptTemplate.REVIEW.scope == Scope.PER_ISSUE


def test_template_merge_has_correct_scope():
    assert PromptTemplate.MERGE.filename == "merge-prompt.md"
    assert PromptTemplate.MERGE.scope == Scope.MERGE


def test_template_plan_has_correct_scope():
    assert PromptTemplate.PLAN.filename == "plan-prompt.md"
    assert PromptTemplate.PLAN.scope == Scope.PLAN


def test_template_preflight_issue_has_correct_scope():
    assert PromptTemplate.PREFLIGHT_ISSUE.filename == "preflight-issue.md"
    assert PromptTemplate.PREFLIGHT_ISSUE.scope == Scope.PREFLIGHT


def test_template_improve_scan_has_correct_scope():
    assert PromptTemplate.IMPROVE_SCAN.filename == "improve/01-scan.md"
    assert PromptTemplate.IMPROVE_SCAN.scope == Scope.IMPROVE_SCAN


def test_template_improve_issues_has_correct_scope():
    assert PromptTemplate.IMPROVE_ISSUES.filename == "improve/03-issues.md"
    assert PromptTemplate.IMPROVE_ISSUES.scope == Scope.IMPROVE_ISSUES


def test_template_resume_has_correct_scope():
    assert PromptTemplate.RESUME.filename == "_resume-prompt.md"
    assert PromptTemplate.RESUME.scope == Scope.RESUME


def test_template_enum_has_eleven_variants():
    assert len(list(PromptTemplate)) == 11


# ── Ctor validates: unknown token raises ─────────────────────────────────────


def test_renderer_ctor_rejects_unknown_token(cfg, prompts_dir):
    (prompts_dir / "plan-prompt.md").write_text(
        "Issues: {{ALL_OPEN_ISSUES_JSON}}\nUnknown: {{XYZZY}}"
    )
    with pytest.raises(PromptRenderError, match="XYZZY"):
        PromptRenderer(cfg)


def test_renderer_ctor_rejects_typo_in_improve_issues_template(cfg, prompts_dir):
    # IMPROVE_ISSUES scope must reject any unknown placeholder at startup.
    (prompts_dir / "improve" / "03-issues.md").write_text(
        "SID: {{IMPROVE_SHORT_SID}}\nNum: {{ISSUE_NUMBR}}"  # typo: NUMBR
    )
    with pytest.raises(PromptRenderError, match="ISSUE_NUMBR"):
        PromptRenderer(cfg)


def test_renderer_ctor_accepts_improve_issues_template(cfg, prompts_dir):
    (prompts_dir / "improve" / "03-issues.md").write_text(
        "Task: #{{ISSUE_NUMBER}} {{ISSUE_TITLE}}\n"
        "Body: {{ISSUE_BODY}}\nComments: {{ISSUE_COMMENTS}}\n"
        "SID: {{IMPROVE_SHORT_SID}}"
    )
    PromptRenderer(cfg)  # must not raise


# ── Ctor validates: out-of-scope token raises ────────────────────────────────


def test_renderer_ctor_rejects_out_of_scope_token(cfg, prompts_dir):
    # merge-prompt.md is MERGE scope; ISSUE_NUMBER is PER_ISSUE scope only
    (prompts_dir / "merge-prompt.md").write_text(
        "Branches: {{BRANCHES}}\nWrong: {{ISSUE_NUMBER}}"
    )
    with pytest.raises(PromptRenderError, match="ISSUE_NUMBER"):
        PromptRenderer(cfg)


# ── Ctor skips missing template files (no error) ─────────────────────────────


def test_renderer_ctor_succeeds_when_template_files_absent(cfg, prompts_dir):
    # No files written — all templates absent, validation is skipped
    PromptRenderer(cfg)  # must not raise


# ── Ctor accepts global tokens in any template ───────────────────────────────


def test_renderer_ctor_accepts_global_token_in_merge_template(cfg, prompts_dir):
    # CHECKS is a global placeholder; it must be valid in MERGE scope
    (prompts_dir / "merge-prompt.md").write_text(
        "Branches: {{BRANCHES}}\nRun: {{CHECKS}}"
    )
    PromptRenderer(cfg)  # must not raise


# ── render: exact match on scope_args required ───────────────────────────────


def test_render_rejects_missing_scope_arg(cfg, prompts_dir):
    (prompts_dir / "plan-prompt.md").write_text(
        "Issues: {{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}"
    )
    renderer = PromptRenderer(cfg)

    with pytest.raises(PromptRenderError, match="missing"):
        _run(renderer.render(PromptTemplate.PLAN, {}, _noop_exec))


def test_render_rejects_extra_scope_arg(cfg, prompts_dir):
    (prompts_dir / "merge-prompt.md").write_text("Branches: {{BRANCHES}}")
    renderer = PromptRenderer(cfg)

    with pytest.raises(PromptRenderError, match="extra"):
        _run(
            renderer.render(
                PromptTemplate.MERGE,
                {"BRANCHES": "- b1", "EXTRA_KEY": "oops"},
                _noop_exec,
            )
        )


def test_render_rejects_typo_in_scope_arg(cfg, prompts_dir):
    (prompts_dir / "plan-prompt.md").write_text(
        "Issues: {{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}"
    )
    renderer = PromptRenderer(cfg)

    with pytest.raises(PromptRenderError):
        _run(
            renderer.render(
                PromptTemplate.PLAN,
                {
                    "ALL_OPEN_ISSUE_JSON": "[]",
                    "READY_FOR_AGENT_ISSUES_JSON": "[]",
                },  # typo in first key
                _noop_exec,
            )
        )


# ── render: unused global args do not produce warnings ───────────────────────


def test_render_does_not_warn_for_unused_global_args(cfg, prompts_dir, capsys):
    # Template uses only scope args; global args are unused and must be silent.
    (prompts_dir / "plan-prompt.md").write_text(
        "All: {{ALL_OPEN_ISSUES_JSON}} Ready: {{READY_FOR_AGENT_ISSUES_JSON}}"
    )
    renderer = PromptRenderer(cfg)

    _run(
        renderer.render(
            PromptTemplate.PLAN,
            {"ALL_OPEN_ISSUES_JSON": "[]", "READY_FOR_AGENT_ISSUES_JSON": "[]"},
            _noop_exec,
        )
    )

    assert capsys.readouterr().err == ""


# ── render: scope_args substituted alongside globals ─────────────────────────


def test_render_substitutes_scope_arg(cfg, prompts_dir):
    (prompts_dir / "plan-prompt.md").write_text(
        "All: {{ALL_OPEN_ISSUES_JSON}} Ready: {{READY_FOR_AGENT_ISSUES_JSON}}"
    )
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.PLAN,
            {"ALL_OPEN_ISSUES_JSON": "[1,2]", "READY_FOR_AGENT_ISSUES_JSON": "[1]"},
            _noop_exec,
        )
    )

    assert result == "All: [1,2] Ready: [1]"


def test_render_resume_scope_accepts_empty_scope_args(cfg, prompts_dir):
    (prompts_dir / "_resume-prompt.md").write_text("Resume.")
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))

    assert result == "Resume."


# ── render: standards placeholders available in IMPROVE_SCAN scope ───────────


def test_render_standards_available_in_improve_scan(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "tests.md").write_text("test guidelines")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{TESTING_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "test guidelines"


# ── render: new language/deepening standards render in IMPROVE_SCAN ──────────


def test_render_language_standards_available_in_improve_scan(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "language.md").write_text("language guidelines")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{LANGUAGE_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "language guidelines"


def test_render_deepening_standards_available_in_improve_scan(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "deepening.md").write_text("deepening guidelines")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{DEEPENING_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "deepening guidelines"


def test_render_language_standards_available_in_improve_prd(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "language.md").write_text("language guidelines")
    (prompts_dir / "improve" / "02-prd.md").write_text("{{LANGUAGE_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_PRD, {"IMPROVE_SHORT_SID": "abc"}, _noop_exec
        )
    )

    assert result == "language guidelines"


def test_render_deepening_standards_available_in_improve_prd(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "deepening.md").write_text("deepening guidelines")
    (prompts_dir / "improve" / "02-prd.md").write_text("{{DEEPENING_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_PRD, {"IMPROVE_SHORT_SID": "abc"}, _noop_exec
        )
    )

    assert result == "deepening guidelines"


# ── Security regressions (from test_prompt_utils.py, adapted for renderer) ───


def test_arg_value_containing_shell_token_is_not_executed(cfg, prompts_dir):
    (prompts_dir / "implement-prompt.md").write_text("Diff:\n{{ISSUE_BODY}}\n")
    renderer = PromptRenderer(cfg)

    calls: list[str] = []

    async def recording_exec(cmd: str) -> str:
        calls.append(cmd)
        return "EXECUTED"

    _run(
        renderer.render(
            PromptTemplate.IMPLEMENT,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "context\n!`shell`\nmore",
                "ISSUE_COMMENTS": "",
                "BRANCH": "b",
            },
            recording_exec,
        )
    )

    assert calls == []
    assert "!`shell`" in _run(
        renderer.render(
            PromptTemplate.IMPLEMENT,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "context\n!`shell`\nmore",
                "ISSUE_COMMENTS": "",
                "BRANCH": "b",
            },
            _noop_exec,
        )
    )


# ── Standards loading: behaviours from the deleted test_prompt_utils.py ──────


def test_renderer_loads_all_seven_standards_keys(prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "tests.md").write_text("testing content")
    (standards_dir / "mocking.md").write_text("mocking content")
    (standards_dir / "interfaces.md").write_text("interfaces content")
    (standards_dir / "deep-modules.md").write_text("deep modules content")
    (standards_dir / "refactoring.md").write_text("refactoring content")
    (standards_dir / "language.md").write_text("language content")
    (standards_dir / "deepening.md").write_text("deepening content")
    (prompts_dir / "improve" / "01-scan.md").write_text(
        "{{TESTING_STANDARDS}}|{{MOCKING_STANDARDS}}|{{INTERFACES_STANDARDS}}"
        "|{{DEEP_MODULES_STANDARDS}}|{{REFACTORING_STANDARDS}}"
        "|{{LANGUAGE_STANDARDS}}|{{DEEPENING_STANDARDS}}"
    )
    cfg = Config(prompts_dir=prompts_dir)
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == (
        "testing content|mocking content|interfaces content"
        "|deep modules content|refactoring content"
        "|language content|deepening content"
    )


def test_renderer_returns_empty_string_for_missing_standards_file(prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "tests.md").write_text("testing content")
    # other files intentionally absent
    (prompts_dir / "improve" / "01-scan.md").write_text(
        "{{TESTING_STANDARDS}}|{{MOCKING_STANDARDS}}"
    )
    cfg = Config(prompts_dir=prompts_dir)
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "testing content|"


def test_renderer_returns_all_empty_standards_when_dir_absent(tmp_path):
    (tmp_path / "improve").mkdir()
    (tmp_path / "improve" / "01-scan.md").write_text(
        "{{TESTING_STANDARDS}}|{{MOCKING_STANDARDS}}"
    )
    # no coding-standards dir created
    cfg = Config(prompts_dir=tmp_path)
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "|"


# ── Template shell expression tests ──────────────────────────────────────────


def test_template_shell_expr_runs_arg_shell_token_stays_inert(cfg, prompts_dir):
    (prompts_dir / "implement-prompt.md").write_text(
        "Header: !`echo hi`\nBody: {{ISSUE_BODY}}\n"
    )
    renderer = PromptRenderer(cfg)

    calls: list[str] = []

    async def recording_exec(cmd: str) -> str:
        calls.append(cmd)
        return "HI"

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "evil payload !`evil`",
                "ISSUE_COMMENTS": "",
                "BRANCH": "b",
            },
            recording_exec,
        )
    )

    assert calls == ["echo hi"]
    assert "Header: HI" in result
    assert "!`evil`" in result
