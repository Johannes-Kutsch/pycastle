import asyncio
import re
from pathlib import Path

import pytest

from pycastle.config import Config
from pycastle.prompts.pipeline import (
    PromptRenderError,
    PromptRenderer,
    PromptTemplate,
    Scope,
    build_wip_clause,
)

_SHIPPED_PROMPTS_DIR = (
    Path(__file__).parent.parent / "src" / "pycastle" / "defaults" / "prompts"
)


async def _noop_exec(cmd: str) -> str:
    return f"output-of:{cmd}"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "improve").mkdir()
    (tmp_path / "coding-standards").mkdir()
    (tmp_path / "implement").mkdir()
    return tmp_path


@pytest.fixture
def cfg(prompts_dir: Path) -> Config:
    return Config(prompts_dir=prompts_dir)


# ── Tracer bullet: renderer renders a global placeholder ──────────────────────


def test_renderer_renders_global_placeholder(cfg, prompts_dir):
    (prompts_dir / "implement" / "behavior.md").write_text(
        "Label: {{READY_FOR_AGENT_LABEL}}"
    )
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "title",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "pycastle/issue-1",
                "WIP_COMMITS": "",
            },
            _noop_exec,
        )
    )

    assert result == "Label: ready-for-agent"


# ── Scope enum has correct placeholder sets ───────────────────────────────────


def test_scope_per_issue_placeholders():
    assert Scope.PER_ISSUE.placeholders == frozenset(
        {
            "ISSUE_NUMBER",
            "ISSUE_TITLE",
            "ISSUE_BODY",
            "ISSUE_COMMENTS",
            "BRANCH",
            "WIP_COMMITS",
        }
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
    assert len(list(Scope)) == 10


# ── PromptTemplate enum has correct filename and scope ────────────────────────


def test_template_implement_behavior_has_correct_filename_and_scope():
    assert PromptTemplate.IMPLEMENT_BEHAVIOR.filename == "implement/behavior.md"
    assert PromptTemplate.IMPLEMENT_BEHAVIOR.scope == Scope.PER_ISSUE


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


def test_template_enum_has_fourteen_variants():
    assert len(list(PromptTemplate)) == 14


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


def test_render_implementation_standards_available_in_improve_scan(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "implementation.md").write_text("implementation guidelines")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{IMPLEMENTATION_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "implementation guidelines"


def test_render_design_standards_available_in_improve_scan(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "design.md").write_text("design guidelines")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{DESIGN_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "design guidelines"


def test_render_design_standards_available_in_improve_prd(cfg, prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "design.md").write_text("design guidelines")
    (prompts_dir / "improve" / "02-prd.md").write_text("{{DESIGN_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_PRD, {"IMPROVE_SHORT_SID": "abc"}, _noop_exec
        )
    )

    assert result == "design guidelines"


def test_render_implement_output_rules_available_in_per_issue_template(
    cfg, prompts_dir
):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "implement-output-rules.md").write_text("output rules content")
    (prompts_dir / "implement" / "behavior.md").write_text("{{IMPLEMENT_OUTPUT_RULES}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "b",
                "WIP_COMMITS": "",
            },
            _noop_exec,
        )
    )

    assert result == "output rules content"


# ── Security regressions (from test_prompt_utils.py, adapted for renderer) ───


def test_arg_value_containing_shell_token_is_not_executed(cfg, prompts_dir):
    (prompts_dir / "implement" / "behavior.md").write_text("Diff:\n{{ISSUE_BODY}}\n")
    renderer = PromptRenderer(cfg)

    calls: list[str] = []

    async def recording_exec(cmd: str) -> str:
        calls.append(cmd)
        return "EXECUTED"

    _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "context\n!`shell`\nmore",
                "ISSUE_COMMENTS": "",
                "BRANCH": "b",
                "WIP_COMMITS": "",
            },
            recording_exec,
        )
    )

    assert calls == []
    assert "!`shell`" in _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "context\n!`shell`\nmore",
                "ISSUE_COMMENTS": "",
                "BRANCH": "b",
                "WIP_COMMITS": "",
            },
            _noop_exec,
        )
    )


# ── Standards loading: behaviours from the deleted test_prompt_utils.py ──────


def test_renderer_loads_both_standards_keys(prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "design.md").write_text("design content")
    (standards_dir / "implementation.md").write_text("implementation content")
    (prompts_dir / "improve" / "01-scan.md").write_text(
        "{{DESIGN_STANDARDS}}|{{IMPLEMENTATION_STANDARDS}}"
    )
    cfg = Config(prompts_dir=prompts_dir)
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "design content|implementation content"


def test_renderer_returns_empty_string_for_missing_standards_file(prompts_dir):
    standards_dir = prompts_dir / "coding-standards"
    (standards_dir / "design.md").write_text("design content")
    # implementation.md intentionally absent
    (prompts_dir / "improve" / "01-scan.md").write_text(
        "{{DESIGN_STANDARDS}}|{{IMPLEMENTATION_STANDARDS}}"
    )
    cfg = Config(prompts_dir=prompts_dir)
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "design content|"


def test_renderer_returns_all_empty_standards_when_dir_absent(tmp_path):
    (tmp_path / "improve").mkdir()
    (tmp_path / "improve" / "01-scan.md").write_text(
        "{{DESIGN_STANDARDS}}|{{IMPLEMENTATION_STANDARDS}}"
    )
    # no coding-standards dir created
    cfg = Config(prompts_dir=tmp_path)
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "|"


def test_renderer_renders_issue_tracker_fragment(prompts_dir):
    (prompts_dir / "_issue-tracker.md").write_text("issue-tracker recipes")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{ISSUE_TRACKER}}")
    cfg = Config(prompts_dir=prompts_dir)
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.IMPROVE_SCAN, {}, _noop_exec))

    assert result == "issue-tracker recipes"


def test_renderer_aborts_when_issue_tracker_referenced_but_absent(prompts_dir):
    (prompts_dir / "improve" / "01-scan.md").write_text("{{ISSUE_TRACKER}}")
    cfg = Config(prompts_dir=prompts_dir)

    with pytest.raises(PromptRenderError, match="ISSUE_TRACKER"):
        PromptRenderer(cfg)


# ── No legacy standards placeholders in defaults-tree prompts ────────────────

_LEGACY_STANDARDS_NAMES = {
    "TESTING_STANDARDS",
    "MOCKING_STANDARDS",
    "INTERFACES_STANDARDS",
    "DEEP_MODULES_STANDARDS",
    "REFACTORING_STANDARDS",
    "LANGUAGE_STANDARDS",
    "DEEPENING_STANDARDS",
}


def test_no_legacy_standards_placeholder_in_defaults_prompts():
    for path in _SHIPPED_PROMPTS_DIR.rglob("*.md"):
        if path.name.startswith("_"):
            continue
        content = path.read_text(encoding="utf-8")
        found = set(re.findall(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", content))
        legacy_found = found & _LEGACY_STANDARDS_NAMES
        assert not legacy_found, (
            f"{path.relative_to(_SHIPPED_PROMPTS_DIR)} references legacy placeholder(s): "
            f"{legacy_found}"
        )


# ── Template shell expression tests ──────────────────────────────────────────


def test_template_shell_expr_runs_arg_shell_token_stays_inert(cfg, prompts_dir):
    (prompts_dir / "implement" / "behavior.md").write_text(
        "Header: !`echo hi`\nBody: {{ISSUE_BODY}}\n"
    )
    renderer = PromptRenderer(cfg)

    calls: list[str] = []

    async def recording_exec(cmd: str) -> str:
        calls.append(cmd)
        return "HI"

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "evil payload !`evil`",
                "ISSUE_COMMENTS": "",
                "BRANCH": "b",
                "WIP_COMMITS": "",
            },
            recording_exec,
        )
    )

    assert calls == ["echo hi"]
    assert "Header: HI" in result
    assert "!`evil`" in result


# ── _placeholder-info.md reference card validation ───────────────────────────

_TOKEN_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def _parse_placeholder_info() -> tuple[set[str], dict[str, tuple[set[str], set[str]]]]:
    """Parse the shipped _placeholder-info.md.

    Returns (global_tokens, {scope_name: (tokens, used_by_filenames)}).
    """
    text = (_SHIPPED_PROMPTS_DIR / "_placeholder-info.md").read_text(encoding="utf-8")
    global_tokens: set[str] = set()
    scopes: dict[str, tuple[set[str], set[str]]] = {}

    for section in re.split(r"(?m)^## ", text)[1:]:
        lines = section.splitlines()
        heading = lines[0].strip()
        body = "\n".join(lines[1:])

        if heading == "Global placeholders":
            global_tokens = set(_TOKEN_RE.findall(body))
        elif heading.startswith("Scope: "):
            scope_name = heading[len("Scope: ") :]
            tokens = set(_TOKEN_RE.findall(body))
            used_by: set[str] = set()
            for line in lines[1:]:
                if line.startswith("Used by:"):
                    used_by = {f.strip() for f in line[len("Used by:") :].split(",")}
                    break
            scopes[scope_name] = (tokens, used_by)

    return global_tokens, scopes


def test_placeholder_info_global_tokens_match_code(cfg, prompts_dir):
    # ISSUE_TRACKER is conditional on its fragment file being present.
    (prompts_dir / "_issue-tracker.md").write_text("issue-tracker recipes")
    renderer = PromptRenderer(cfg)
    expected = set(renderer._global_args.keys())

    global_tokens, _ = _parse_placeholder_info()

    assert global_tokens == expected


def test_placeholder_info_scope_tokens_match_code():
    _, scopes = _parse_placeholder_info()

    for scope in Scope:
        assert scope.name in scopes, f"Missing section for Scope.{scope.name}"
        tokens, _ = scopes[scope.name]
        assert tokens == scope.placeholders, (
            f"Scope {scope.name}: file tokens {tokens!r} != code {scope.placeholders!r}"
        )


def test_placeholder_info_used_by_lines_match_code():
    _, scopes = _parse_placeholder_info()

    for scope in Scope:
        _, used_by = scopes[scope.name]
        expected = {t.filename for t in PromptTemplate if t.scope is scope}
        assert used_by == expected, (
            f"Scope {scope.name}: Used by {used_by!r} != expected {expected!r}"
        )


def test_placeholder_info_no_unknown_scope_sections():
    _, scopes = _parse_placeholder_info()
    known = {s.name for s in Scope}

    for name in scopes:
        assert name in known, f"Unknown scope section: ## Scope: {name}"


# ── FAILURE_REPORT scope ──────────────────────────────────────────────────────


def test_scope_failure_report_placeholders():
    assert Scope.FAILURE_REPORT.placeholders == frozenset(
        {"FAILED_ROLE", "SESSION_DIR", "FAILURE_CLASS"}
    )


# ── failure-report.md conditional rendering ───────────────────────────────────

_FAILURE_REPORT_SCOPE_ARGS_BASE = {
    "FAILED_ROLE": "implementer",
    "SESSION_DIR": "/sessions/abc",
}


def _make_failure_report_renderer() -> PromptRenderer:
    from pycastle.config import Config

    cfg = Config(prompts_dir=_SHIPPED_PROMPTS_DIR)
    return PromptRenderer(cfg)


def test_failure_report_renders_recovery_section_for_non_typed_crash():
    renderer = _make_failure_report_renderer()

    result = _run(
        renderer.render(
            PromptTemplate.FAILURE_REPORT,
            {**_FAILURE_REPORT_SCOPE_ARGS_BASE, "FAILURE_CLASS": "non_typed_crash"},
            _noop_exec,
        )
    )

    assert "## Recovery" in result
    assert "rm -rf <SESSION_DIR>" in result


def test_failure_report_omits_recovery_section_for_protocol_error():
    renderer = _make_failure_report_renderer()

    result = _run(
        renderer.render(
            PromptTemplate.FAILURE_REPORT,
            {**_FAILURE_REPORT_SCOPE_ARGS_BASE, "FAILURE_CLASS": "protocol_error"},
            _noop_exec,
        )
    )

    assert "## Recovery" not in result
    assert "rm -rf <SESSION_DIR>" not in result


# ── Conditional block rendering ───────────────────────────────────────────────


def test_conditional_block_renders_when_condition_matches(cfg, prompts_dir):
    (prompts_dir / "failure-report.md").write_text(
        "Before\n{{#if FAILURE_CLASS=non_typed_crash}}\nSection\n{{/if}}\nAfter"
    )
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.FAILURE_REPORT,
            {
                "FAILED_ROLE": "r",
                "SESSION_DIR": "/s",
                "FAILURE_CLASS": "non_typed_crash",
            },
            _noop_exec,
        )
    )

    assert "Section" in result
    assert "Before" in result
    assert "After" in result


def test_conditional_block_omitted_when_condition_does_not_match(cfg, prompts_dir):
    (prompts_dir / "failure-report.md").write_text(
        "Before\n{{#if FAILURE_CLASS=non_typed_crash}}\nSection\n{{/if}}\nAfter"
    )
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.FAILURE_REPORT,
            {
                "FAILED_ROLE": "r",
                "SESSION_DIR": "/s",
                "FAILURE_CLASS": "protocol_error",
            },
            _noop_exec,
        )
    )

    assert "Section" not in result
    assert "Before" in result
    assert "After" in result


def test_renderer_ctor_rejects_out_of_scope_conditional_key(cfg, prompts_dir):
    (prompts_dir / "failure-report.md").write_text(
        "{{#if UNKNOWN_KEY=value}}\nContent\n{{/if}}"
    )
    with pytest.raises(PromptRenderError, match="UNKNOWN_KEY"):
        PromptRenderer(cfg)


# ── WIP_COMMITS scope placeholder ────────────────────────────────────────────


def test_scope_per_issue_includes_wip_commits():
    assert "WIP_COMMITS" in Scope.PER_ISSUE.placeholders


_PER_ISSUE_BASE = {
    "ISSUE_NUMBER": "42",
    "ISSUE_TITLE": "Fix bug",
    "ISSUE_BODY": "",
    "ISSUE_COMMENTS": "",
    "BRANCH": "pycastle/issue-42",
}


# ── build_wip_clause: four combinations ──────────────────────────────────────


def test_wip_clause_present_when_commits_exist_and_service_not_resumable():
    subjects = ["WIP: implementer #42 - interrupted"]
    result = build_wip_clause(subjects, False, role="implementer", issue_number=42)
    assert "WIP Context" in result
    assert "WIP: implementer #42 - interrupted" in result


def test_wip_clause_absent_when_commits_exist_but_service_is_resumable():
    subjects = ["WIP: implementer #42 - interrupted"]
    result = build_wip_clause(subjects, True, role="implementer", issue_number=42)
    assert result == ""


def test_wip_clause_absent_when_no_commits_and_service_not_resumable():
    result = build_wip_clause([], False, role="implementer", issue_number=42)
    assert result == ""


def test_wip_clause_absent_when_no_commits_and_service_is_resumable():
    result = build_wip_clause([], True, role="implementer", issue_number=42)
    assert result == ""


# ── build_wip_clause: commit filtering ───────────────────────────────────────


def test_wip_clause_filters_by_role():
    subjects = [
        "WIP: reviewer #42 - interrupted",
        "WIP: implementer #42 - interrupted",
    ]
    result = build_wip_clause(subjects, False, role="implementer", issue_number=42)
    assert "WIP: implementer #42 - interrupted" in result
    assert "WIP: reviewer #42 - interrupted" not in result


def test_wip_clause_filters_by_issue_number():
    subjects = [
        "WIP: implementer #99 - interrupted",
        "WIP: implementer #42 - interrupted",
    ]
    result = build_wip_clause(subjects, False, role="implementer", issue_number=42)
    assert "WIP: implementer #42 - interrupted" in result
    assert "WIP: implementer #99 - interrupted" not in result


# ── WIP_COMMITS rendered in prompt ────────────────────────────────────────────


def test_render_includes_wip_clause_when_wip_commits_non_empty(cfg, prompts_dir):
    (prompts_dir / "implement" / "behavior.md").write_text(
        "Context:{{WIP_COMMITS}}Done"
    )
    renderer = PromptRenderer(cfg)
    wip = build_wip_clause(
        ["WIP: implementer #1 - interrupted"], False, role="implementer", issue_number=1
    )

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {**_PER_ISSUE_BASE, "ISSUE_NUMBER": "1", "WIP_COMMITS": wip},
            _noop_exec,
        )
    )

    assert "WIP Context" in result


def test_render_omits_wip_clause_when_wip_commits_empty(cfg, prompts_dir):
    (prompts_dir / "implement" / "behavior.md").write_text(
        "Context:{{WIP_COMMITS}}Done"
    )
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {**_PER_ISSUE_BASE, "WIP_COMMITS": ""},
            _noop_exec,
        )
    )

    assert "WIP Context" not in result
    assert result == "Context:Done"


# ── diverge-prompt.md contract ────────────────────────────────────────────────

_DIVERGE_PROMPT = (_SHIPPED_PROMPTS_DIR / "diverge-prompt.md").read_text()


def test_diverge_prompt_does_not_contain_checks_placeholder():
    assert "{{CHECKS}}" not in _DIVERGE_PROMPT


def test_diverge_prompt_instructs_resolver_not_to_run_preflight_checks():
    assert "preflight" in _DIVERGE_PROMPT.lower()


def test_diverge_prompt_defines_complete_as_merge_committed_cleanly():
    assert "<promise>COMPLETE</promise>" in _DIVERGE_PROMPT
    complete_idx = _DIVERGE_PROMPT.index("<promise>COMPLETE</promise>")
    context = _DIVERGE_PROMPT[max(0, complete_idx - 120) : complete_idx + 120].lower()
    assert "clean" in context


def test_diverge_prompt_defines_failed_as_conflicts_cannot_be_resolved_textually():
    assert "<promise>FAILED</promise>" in _DIVERGE_PROMPT
    failed_idx = _DIVERGE_PROMPT.index("<promise>FAILED</promise>")
    context = _DIVERGE_PROMPT[max(0, failed_idx - 120) : failed_idx + 120].lower()
    assert "textual" in context or "conflict" in context
