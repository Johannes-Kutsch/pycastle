import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from pycastle.config import Config
from pycastle.label_catalog import PROMPT_GLOBAL_LABEL_SPECS
from pycastle.prompts.pipeline import (
    PromptRenderError,
    PromptRenderer,
    PromptTemplate,
    Scope,
)
from pycastle.prompts.scope_args import (
    build_interrupted_work_clause,
    validated_scope_args_for_template,
)
from pycastle.prompts.source import PromptReference, PromptSource
from pycastle.session import RunKind

_SHIPPED_PROMPTS_DIR = (
    Path(__file__).parent.parent / "src" / "pycastle" / "defaults" / "prompts"
)


async def _noop_exec(cmd: str) -> str:
    return f"output-of:{cmd}"


def _run(coro):
    return asyncio.run(coro)


def _symlink_to_or_skip(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable (winerror 1314)")
        raise


def _cfg_for_prompts_dir(prompts_dir: Path) -> SimpleNamespace:
    base = Config()
    return SimpleNamespace(
        prompts_dir=prompts_dir,
        preflight_checks=base.preflight_checks,
        bug_label=base.bug_label,
        issue_label=base.issue_label,
        hitl_label=base.hitl_label,
        enhancement_label=base.enhancement_label,
        needs_triage_label=base.needs_triage_label,
        needs_info_label=base.needs_info_label,
        wontfix_label=base.wontfix_label,
        refactor_slice_label=base.refactor_slice_label,
        behavior_slice_label=base.behavior_slice_label,
        docs_slice_label=base.docs_slice_label,
        implement_checks=base.implement_checks,
    )


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "improve").mkdir(parents=True)
    (prompts_dir / "shared/standards").mkdir(parents=True)
    (prompts_dir / "work").mkdir()
    (prompts_dir / "coordination").mkdir()
    (prompts_dir / "diagnostics").mkdir()
    return prompts_dir


@pytest.fixture
def cfg(prompts_dir: Path) -> Config:
    return Config()


# ── Tracer bullet: renderer renders a global placeholder ──────────────────────


def test_renderer_renders_global_placeholder(cfg, prompts_dir):
    (prompts_dir / "work" / "behavior.md").write_text(
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
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    assert result == "Label: ready-for-agent"


def test_renderer_preserves_all_label_global_placeholders(cfg, prompts_dir):
    (prompts_dir / "work" / "behavior.md").write_text(
        "\n".join(
            (
                "BUG={{BUG_LABEL}}",
                "READY={{READY_FOR_AGENT_LABEL}}",
                "HITL={{READY_FOR_HUMAN_LABEL}}",
                "ENHANCEMENT={{ENHANCEMENT_LABEL}}",
                "TRIAGE={{NEEDS_TRIAGE_LABEL}}",
                "INFO={{NEEDS_INFO_LABEL}}",
                "WONTFIX={{WONTFIX_LABEL}}",
                "REFACTOR={{REFACTOR_SLICE_LABEL}}",
                "BEHAVIOR={{BEHAVIOR_SLICE_LABEL}}",
                "DOCS={{DOCS_SLICE_LABEL}}",
            )
        )
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
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    assert result == "\n".join(
        (
            "BUG=bug",
            "READY=ready-for-agent",
            "HITL=ready-for-human",
            "ENHANCEMENT=enhancement",
            "TRIAGE=needs-triage",
            "INFO=needs-info",
            "WONTFIX=wontfix",
            "REFACTOR=refactor-slice",
            "BEHAVIOR=behavior-slice",
            "DOCS=docs-slice",
        )
    )


def test_renderer_uses_fixed_project_local_prompt_overrides_when_config_is_stale(
    tmp_path: Path,
):
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "work").mkdir(parents=True)
    (prompts_dir / "work" / "behavior.md").write_text("Fixed local prompt override")
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "title",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "pycastle/issue-1",
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    assert result == "Fixed local prompt override"


def test_renderer_uses_bundled_prompt_when_config_is_stale_and_local_prompt_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    renderer = PromptRenderer(Config())
    shipped_renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))
    shipped_result = _run(
        shipped_renderer.render(PromptTemplate.RESUME, {}, _noop_exec)
    )

    assert result == shipped_result


# ── Scope enum has correct placeholder sets ───────────────────────────────────


def test_scope_per_issue_placeholders():
    assert Scope.PER_ISSUE.placeholders == frozenset(
        {
            "ISSUE_NUMBER",
            "ISSUE_TITLE",
            "ISSUE_BODY",
            "ISSUE_COMMENTS",
            "BRANCH",
            "INTERRUPTED_WORK",
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


def test_scope_host_check_placeholders():
    assert Scope.HOST_CHECK.placeholders == frozenset(
        {
            "HOST_OS",
            "HOST_PLATFORM",
            "CHECKED_SHA",
            "CHECK_NAME",
            "COMMAND",
            "OUTPUT",
        }
    )


def test_scope_improve_scan_placeholders():
    assert Scope.IMPROVE_SCAN.placeholders == frozenset({"RECENT_IMPROVE_PRD_TITLES"})


def test_scope_improve_session_placeholders():
    assert Scope.IMPROVE_SESSION.placeholders == frozenset(
        {"IMPROVE_SHORT_SID", "RECENT_IMPROVE_PRDS"}
    )


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
    assert len(list(Scope)) == 11


# ── PromptTemplate enum has correct filename and scope ────────────────────────


@pytest.mark.parametrize(
    ("template", "expected_filename", "expected_scope"),
    [
        (PromptTemplate.IMPLEMENT_BEHAVIOR, "work/behavior.md", Scope.PER_ISSUE),
        (PromptTemplate.IMPLEMENT_REFACTOR, "work/refactor.md", Scope.PER_ISSUE),
        (PromptTemplate.IMPLEMENT_DOCS, "work/docs.md", Scope.PER_ISSUE),
        (PromptTemplate.REVIEW, "work/review.md", Scope.PER_ISSUE),
        (PromptTemplate.MERGE, "coordination/merge.md", Scope.MERGE),
        (PromptTemplate.PLAN, "coordination/plan.md", Scope.PLAN),
        (
            PromptTemplate.PREFLIGHT_ISSUE,
            "diagnostics/preflight-issue.md",
            Scope.PREFLIGHT,
        ),
        (
            PromptTemplate.HOST_CHECK_ISSUE,
            "diagnostics/host-check-issue.md",
            Scope.HOST_CHECK,
        ),
        (PromptTemplate.IMPROVE_SCAN, "improve/01-scan.md", Scope.IMPROVE_SCAN),
        (PromptTemplate.IMPROVE_PRD, "improve/02-prd.md", Scope.IMPROVE_SESSION),
        (PromptTemplate.IMPROVE_ISSUES, "improve/03-issues.md", Scope.IMPROVE_ISSUES),
        (
            PromptTemplate.IMPROVE_NO_CANDIDATE,
            "improve/04-no-candidate-report.md",
            Scope.IMPROVE_SESSION,
        ),
        (PromptTemplate.RESUME, "shared/resume.md", Scope.RESUME),
        (
            PromptTemplate.DIVERGENCE_RESOLVE,
            "coordination/diverge.md",
            Scope.DIVERGE,
        ),
        (
            PromptTemplate.FAILURE_REPORT,
            "diagnostics/failure-report.md",
            Scope.FAILURE_REPORT,
        ),
    ],
)
def test_template_filename_and_scope(template, expected_filename, expected_scope):
    assert template.filename == expected_filename
    assert template.scope == expected_scope


def test_template_reference_carries_name_and_relative_path():
    ref = PromptTemplate.IMPLEMENT_BEHAVIOR.reference
    assert isinstance(ref, PromptReference)
    assert ref.name == "IMPLEMENT_BEHAVIOR"
    assert ref.relative_path == PromptTemplate.IMPLEMENT_BEHAVIOR.filename


def test_template_enum_has_fifteen_variants():
    assert len(list(PromptTemplate)) == 15


# ── Ctor validates: unknown token raises ─────────────────────────────────────


def test_renderer_ctor_rejects_unknown_token(cfg, prompts_dir):
    (prompts_dir / "coordination/plan.md").write_text(
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
    # coordination/merge.md is MERGE scope; ISSUE_NUMBER is PER_ISSUE scope only
    (prompts_dir / "coordination/merge.md").write_text(
        "Branches: {{BRANCHES}}\nWrong: {{ISSUE_NUMBER}}"
    )
    with pytest.raises(PromptRenderError, match="ISSUE_NUMBER"):
        PromptRenderer(cfg)


def test_renderer_ctor_rejects_broken_effective_local_role_prompt_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/resume.md").write_text("Broken: {{ISSUE_NUMBER}}")

    with pytest.raises(PromptRenderError, match="ISSUE_NUMBER"):
        PromptRenderer(Config())


def test_renderer_ctor_rejects_unknown_fixed_local_prompt_override_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "work").mkdir(parents=True)
    (prompts_dir / "work" / "scratch.md").write_text("unused")

    with pytest.raises(PromptRenderError, match=r"work/scratch\.md"):
        PromptRenderer(Config())


def test_renderer_ctor_rejects_stale_flat_local_prompt_override_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "implement.md").write_text("stale flat prompt override")

    with pytest.raises(PromptRenderError, match=r"implement\.md"):
        PromptRenderer(Config())


def test_renderer_ctor_allows_extra_directories_in_fixed_local_prompt_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "notes/archive").mkdir(parents=True)

    PromptRenderer(Config())


# ── Ctor skips missing template files (no error) ─────────────────────────────


def test_renderer_ctor_succeeds_when_template_files_absent(cfg, prompts_dir):
    # No files written — all templates absent, validation is skipped
    PromptRenderer(cfg)  # must not raise


# ── Ctor accepts global tokens in any template ───────────────────────────────


def test_renderer_ctor_accepts_global_token_in_merge_template(cfg, prompts_dir):
    # CHECKS is a global placeholder; it must be valid in MERGE scope
    (prompts_dir / "coordination/merge.md").write_text(
        "Branches: {{BRANCHES}}\nRun: {{CHECKS}}"
    )
    PromptRenderer(cfg)  # must not raise


# ── render: exact match on scope_args required ───────────────────────────────


def test_render_rejects_missing_scope_arg(cfg, prompts_dir):
    (prompts_dir / "coordination/plan.md").write_text(
        "Issues: {{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}"
    )
    renderer = PromptRenderer(cfg)

    with pytest.raises(PromptRenderError, match="missing"):
        _run(renderer.render(PromptTemplate.PLAN, {}, _noop_exec))


def test_render_rejects_extra_scope_arg(cfg, prompts_dir):
    (prompts_dir / "coordination/merge.md").write_text("Branches: {{BRANCHES}}")
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
    (prompts_dir / "coordination/plan.md").write_text(
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


def test_render_accepts_publicly_validated_scope_args(cfg, prompts_dir):
    (prompts_dir / "coordination/plan.md").write_text(
        "Issues: {{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}"
    )
    renderer = PromptRenderer(cfg)
    scope_args = {
        "ALL_OPEN_ISSUES_JSON": "[]",
        "READY_FOR_AGENT_ISSUES_JSON": "[]",
    }

    result = _run(
        renderer.render(
            PromptTemplate.PLAN,
            validated_scope_args_for_template(PromptTemplate.PLAN, scope_args),
            _noop_exec,
        )
    )

    assert result == "Issues: [] []"


# ── render: unused global args do not produce warnings ───────────────────────


def test_render_does_not_warn_for_unused_global_args(cfg, prompts_dir, capsys):
    # Template uses only scope args; global args are unused and must be silent.
    (prompts_dir / "coordination/plan.md").write_text(
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
    (prompts_dir / "coordination/plan.md").write_text(
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
    (prompts_dir / "shared/resume.md").write_text("Resume.")
    renderer = PromptRenderer(cfg)

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))

    assert result == "Resume."


# ── render: standards placeholders available in IMPROVE_SCAN scope ───────────


def test_render_implementation_standards_available_in_improve_scan(cfg, prompts_dir):
    standards_dir = prompts_dir / "shared/standards"
    (standards_dir / "_implementation.md").write_text("implementation guidelines")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{IMPLEMENTATION_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            _noop_exec,
        )
    )

    assert result == "implementation guidelines"


def test_render_design_standards_available_in_improve_scan(cfg, prompts_dir):
    standards_dir = prompts_dir / "shared/standards"
    (standards_dir / "_design.md").write_text("design guidelines")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{DESIGN_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            _noop_exec,
        )
    )

    assert result == "design guidelines"


def test_render_recent_improve_prd_titles_available_in_improve_scan(cfg, prompts_dir):
    (prompts_dir / "improve" / "01-scan.md").write_text(
        "recent:\n{{RECENT_IMPROVE_PRD_TITLES}}"
    )
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {
                "RECENT_IMPROVE_PRD_TITLES": "#12 OPEN - First candidate",
            },
            _noop_exec,
        )
    )

    assert result == "recent:\n#12 OPEN - First candidate"


def test_render_design_standards_available_in_improve_prd(cfg, prompts_dir):
    standards_dir = prompts_dir / "shared/standards"
    (standards_dir / "_design.md").write_text("design guidelines")
    (prompts_dir / "improve" / "02-prd.md").write_text("{{DESIGN_STANDARDS}}")
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_PRD,
            {
                "IMPROVE_SHORT_SID": "abc",
                "RECENT_IMPROVE_PRDS": "No recent improve PRDs found.",
            },
            _noop_exec,
        )
    )

    assert result == "design guidelines"


def test_render_implement_output_rules_available_in_per_issue_template(
    cfg, prompts_dir
):
    (prompts_dir / "work" / "_output-rules.md").write_text("output rules content")
    (prompts_dir / "work" / "behavior.md").write_text("{{IMPLEMENT_OUTPUT_RULES}}")
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
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    assert result == "output rules content"


# ── Security regressions (from test_prompt_utils.py, adapted for renderer) ───


def test_arg_value_containing_shell_token_is_not_executed(cfg, prompts_dir):
    (prompts_dir / "work" / "behavior.md").write_text("Diff:\n{{ISSUE_BODY}}\n")
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
                "INTERRUPTED_WORK": "",
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
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )


# ── Standards loading: behaviours from the deleted test_prompt_utils.py ──────


def test_renderer_loads_both_standards_keys(prompts_dir):
    standards_dir = prompts_dir / "shared/standards"
    (standards_dir / "_design.md").write_text("design content")
    (standards_dir / "_implementation.md").write_text("implementation content")
    (prompts_dir / "improve" / "01-scan.md").write_text(
        "{{DESIGN_STANDARDS}}|{{IMPLEMENTATION_STANDARDS}}"
    )
    cfg = Config()
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            _noop_exec,
        )
    )

    assert result == "design content|implementation content"


def test_renderer_returns_empty_string_for_missing_standards_file(prompts_dir):
    custom_prompts_dir = prompts_dir.parent / "custom-prompts"
    (custom_prompts_dir / "improve").mkdir(parents=True)
    standards_dir = custom_prompts_dir / "shared/standards"
    standards_dir.mkdir(parents=True)
    (standards_dir / "_design.md").write_text("design content")
    # _implementation.md intentionally absent
    (custom_prompts_dir / "improve" / "01-scan.md").write_text(
        "{{DESIGN_STANDARDS}}|{{IMPLEMENTATION_STANDARDS}}"
    )
    cfg = _cfg_for_prompts_dir(custom_prompts_dir)
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            _noop_exec,
        )
    )

    assert result == "design content|"


def test_renderer_returns_all_empty_standards_when_dir_absent(tmp_path):
    prompts_dir = tmp_path / "custom-prompts"
    (prompts_dir / "improve").mkdir(parents=True)
    (prompts_dir / "improve" / "01-scan.md").write_text(
        "{{DESIGN_STANDARDS}}|{{IMPLEMENTATION_STANDARDS}}"
    )
    # no shared/standards dir created
    cfg = _cfg_for_prompts_dir(prompts_dir)
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            _noop_exec,
        )
    )

    assert result == "|"


def test_renderer_ignores_broken_unreferenced_local_shared_file_in_custom_prompt_tree(
    prompts_dir,
):
    custom_prompts_dir = prompts_dir.parent / "custom-prompts"
    (custom_prompts_dir / "work").mkdir(parents=True)
    (custom_prompts_dir / "shared/standards").mkdir(parents=True)
    (custom_prompts_dir / "work" / "behavior.md").write_text("issue {{ISSUE_NUMBER}}")
    (custom_prompts_dir / "shared/standards" / "_design.md").write_text(
        "{{UNKNOWN_KEY}}"
    )
    cfg = _cfg_for_prompts_dir(custom_prompts_dir)

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
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    assert result == "issue 1"


def test_renderer_allows_extra_files_in_custom_complete_prompt_root(tmp_path: Path):
    custom_prompts_dir = tmp_path / "custom-prompts"
    (custom_prompts_dir / "shared").mkdir(parents=True)
    (custom_prompts_dir / "shared" / "resume.md").write_text("Custom resume")
    (custom_prompts_dir / "scratch.md").write_text("unused")

    renderer = PromptRenderer(_cfg_for_prompts_dir(custom_prompts_dir))

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))

    assert result == "Custom resume"


def test_renderer_renders_issue_tracker_fragment(prompts_dir):
    (prompts_dir / "shared/_issue-tracker.md").write_text("issue-tracker recipes")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{ISSUE_TRACKER}}")
    cfg = Config()
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            _noop_exec,
        )
    )

    assert result == "issue-tracker recipes"


def test_renderer_renders_implement_review_shared_framing_fragment(prompts_dir):
    (prompts_dir / "work/_shared-instructions.md").write_text(
        "branch {{BRANCH}} body {{ISSUE_BODY}}"
    )
    (prompts_dir / "work/review.md").write_text(
        "prefix {{WORK_SHARED_INSTRUCTIONS}} suffix"
    )
    cfg = Config()
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.REVIEW,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "issue body",
                "ISSUE_COMMENTS": "",
                "BRANCH": "pycastle/issue-1",
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    assert result == "prefix branch pycastle/issue-1 body issue body suffix"


def test_renderer_renders_local_issue_tracker_override_through_bundled_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/_issue-tracker.md").write_text(
        "local tracker for {{READY_FOR_AGENT_LABEL}}"
    )
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_NO_CANDIDATE,
            {
                "IMPROVE_SHORT_SID": "abc",
                "RECENT_IMPROVE_PRDS": "No recent improve PRDs found.",
            },
            _noop_exec,
        )
    )

    assert "local tracker for ready-for-agent" in result
    assert "{{READY_FOR_AGENT_LABEL}}" not in result


def test_renderer_allows_empty_local_issue_tracker_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/_issue-tracker.md").write_text("")
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_NO_CANDIDATE,
            {
                "IMPROVE_SHORT_SID": "abc",
                "RECENT_IMPROVE_PRDS": "No recent improve PRDs found.",
            },
            _noop_exec,
        )
    )

    bundled_tracker = (_SHIPPED_PROMPTS_DIR / "shared/_issue-tracker.md").read_text(
        encoding="utf-8"
    )

    assert bundled_tracker not in result


def test_renderer_renders_local_shared_framing_override_through_bundled_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "work").mkdir()
    (prompts_dir / "work/_shared-instructions.md").write_text(
        "shared branch {{BRANCH}}"
    )
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.REVIEW,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "pycastle/issue-1",
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    assert "shared branch pycastle/issue-1" in result
    assert "{{BRANCH}}" not in result


def test_renderer_aborts_on_broken_local_shared_framing_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "work").mkdir()
    (prompts_dir / "work/_shared-instructions.md").write_text("{{UNKNOWN_KEY}}")

    with pytest.raises(PromptRenderError, match="UNKNOWN_KEY"):
        PromptRenderer(Config())


def test_renderer_allows_empty_local_shared_framing_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "work").mkdir()
    (prompts_dir / "work/_shared-instructions.md").write_text("")
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.REVIEW,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "t",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "pycastle/issue-1",
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )

    bundled_framing = (_SHIPPED_PROMPTS_DIR / "work/_shared-instructions.md").read_text(
        encoding="utf-8"
    )

    assert bundled_framing not in result


def test_renderer_aborts_when_shared_framing_referenced_but_absent(prompts_dir):
    custom_prompts_dir = prompts_dir.parent / "custom-prompts"
    (custom_prompts_dir / "work").mkdir(parents=True)
    (custom_prompts_dir / "work/review.md").write_text("{{WORK_SHARED_INSTRUCTIONS}}")
    cfg = _cfg_for_prompts_dir(custom_prompts_dir)

    with pytest.raises(PromptRenderError, match="work/_shared-instructions"):
        PromptRenderer(cfg)


def test_renderer_aborts_when_issue_tracker_referenced_but_absent(prompts_dir):
    custom_prompts_dir = prompts_dir.parent / "custom-prompts"
    (custom_prompts_dir / "improve").mkdir(parents=True)
    (custom_prompts_dir / "improve" / "01-scan.md").write_text("{{ISSUE_TRACKER}}")
    cfg = _cfg_for_prompts_dir(custom_prompts_dir)

    with pytest.raises(PromptRenderError, match="ISSUE_TRACKER"):
        PromptRenderer(cfg)


def test_renderer_aborts_on_broken_local_issue_tracker_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/_issue-tracker.md").write_text(
        "{{#if UNKNOWN_KEY=value}}\nbroken\n{{/if}}"
    )

    with pytest.raises(PromptRenderError, match="UNKNOWN_KEY"):
        PromptRenderer(Config())


def test_renderer_aborts_on_broken_local_coding_standards_override(prompts_dir):
    (prompts_dir / "improve" / "01-scan.md").write_text("{{DESIGN_STANDARDS}}")
    (prompts_dir / "shared/standards" / "_design.md").write_text("{{UNKNOWN_KEY}}")

    with pytest.raises(PromptRenderError, match="UNKNOWN_KEY"):
        PromptRenderer(Config())


def test_renderer_validates_shared_fragment_against_each_referencing_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts" / "shared/standards"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "_implementation.md").write_text("branch {{BRANCH}}")

    with pytest.raises(PromptRenderError, match="BRANCH"):
        PromptRenderer(Config())


def test_renderer_aborts_on_fragment_cycle(prompts_dir):
    (prompts_dir / "shared/_issue-tracker.md").write_text(
        "{{WORK_SHARED_INSTRUCTIONS}}"
    )
    (prompts_dir / "work/_shared-instructions.md").write_text("{{ISSUE_TRACKER}}")
    (prompts_dir / "improve" / "01-scan.md").write_text("{{ISSUE_TRACKER}}")

    with pytest.raises(PromptRenderError, match="cycle"):
        PromptRenderer(Config())


def test_renderer_uses_bundled_prompt_when_default_local_prompt_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    renderer = PromptRenderer(Config())
    shipped_renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))
    shipped_result = _run(
        shipped_renderer.render(PromptTemplate.RESUME, {}, _noop_exec)
    )

    assert result == shipped_result


def test_renderer_uses_bundled_prompt_when_absolute_local_prompts_dir_is_absent(
    tmp_path,
):
    prompts_dir = tmp_path / "pycastle" / "prompts"
    renderer = PromptRenderer(_cfg_for_prompts_dir(prompts_dir))
    shipped_renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))
    shipped_result = _run(
        shipped_renderer.render(PromptTemplate.RESUME, {}, _noop_exec)
    )

    assert result == shipped_result


def test_prompt_source_ignores_unknown_local_file_when_bundled_fallback_is_enabled(
    tmp_path: Path,
):
    local_dir = tmp_path / "pycastle" / "prompts"
    bundled_dir = tmp_path / "bundled-prompts"
    local_dir.mkdir(parents=True)
    bundled_dir.mkdir()
    (local_dir / "unknown.md").write_text("stale local prompt")

    source = PromptSource(local_dir, bundled_dir=bundled_dir)

    assert source.maybe_read_text("unknown.md") is None


def test_renderer_ctor_rejects_broken_unknown_local_prompt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "unknown.md").write_text("stale {{UNKNOWN_TOKEN}}")

    with pytest.raises(PromptRenderError, match="unknown.md"):
        PromptRenderer(Config())


def test_prompt_source_ignores_stale_local_file_for_removed_bundled_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "shared/standards").mkdir(parents=True)
    (prompts_dir / "shared/standards" / "testing.md").write_text(
        "stale local testing standards"
    )

    source = PromptSource.for_prompts_dir(prompts_dir)

    assert source.maybe_read_text("shared/standards/testing.md") is None


def test_prompt_source_only_shadows_for_known_bundled_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/resume.md").write_text("local resume prompt")

    source = PromptSource.for_prompts_dir(prompts_dir)

    assert source.read_text("shared/resume.md") == "local resume prompt"


def test_prompt_source_normalizes_windows_style_bundled_relative_paths(
    tmp_path: Path,
):
    local_dir = tmp_path / "pycastle" / "prompts"
    bundled_dir = tmp_path / "bundled-prompts"
    (local_dir / "shared").mkdir(parents=True)
    (bundled_dir / "shared").mkdir(parents=True)
    (local_dir / "shared" / "resume.md").write_text("local resume prompt")
    (bundled_dir / "shared" / "resume.md").write_text("bundled resume prompt")
    (local_dir / "work").mkdir()
    (bundled_dir / "work").mkdir()
    (local_dir / "work" / "behavior.md").write_text(
        "local behavior prompt {{ISSUE_NUMBER}}"
    )
    (local_dir / "work" / "scratch.md").write_text("stale local prompt")
    (bundled_dir / "work" / "behavior.md").write_text("bundled behavior prompt")

    source = PromptSource(local_dir, bundled_dir=bundled_dir)

    assert source.read_text("shared/resume.md") == "local resume prompt"
    assert (
        source.read_text("work/behavior.md") == "local behavior prompt {{ISSUE_NUMBER}}"
    )
    assert source.unknown_local_relative_paths() == ("work/scratch.md",)


def test_renderer_startup_rejects_unknown_local_prompt_notes(tmp_path: Path):
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "notes.md").write_text("scratchpad {{UNKNOWN_KEY}}")

    with pytest.raises(PromptRenderError, match="notes.md"):
        PromptRenderer(_cfg_for_prompts_dir(prompts_dir))


def test_renderer_startup_rejects_unknown_local_prompt_notes_in_default_local_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "notes.md").write_text("scratchpad {{UNKNOWN_KEY}}")

    with pytest.raises(PromptRenderError, match="notes.md"):
        PromptRenderer(Config())


def test_renderer_rejects_stale_local_prompt_file_not_in_bundled_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared/standards").mkdir(parents=True)
    (prompts_dir / "shared/standards" / "testing.md").write_text(
        "stale testing notes {{UNKNOWN_KEY}}"
    )

    with pytest.raises(PromptRenderError, match="shared/standards/testing.md"):
        PromptRenderer(Config())


def test_renderer_prefers_local_override_over_bundled_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/resume.md").write_text("local resume prompt")
    renderer = PromptRenderer(Config())

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))

    assert result == "local resume prompt"


def test_renderer_falls_back_to_bundled_prompt_when_local_override_path_is_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/resume.md").mkdir()
    renderer = PromptRenderer(Config())
    shipped_renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))
    shipped_result = _run(
        shipped_renderer.render(PromptTemplate.RESUME, {}, _noop_exec)
    )

    assert result == shipped_result


def test_renderer_falls_back_to_bundled_prompt_when_local_override_path_is_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    target = tmp_path / "override.md"
    target.write_text("symlinked local resume prompt")
    _symlink_to_or_skip(prompts_dir / "shared/resume.md", target)
    renderer = PromptRenderer(Config())
    shipped_renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))
    shipped_result = _run(
        shipped_renderer.render(PromptTemplate.RESUME, {}, _noop_exec)
    )

    assert result == shipped_result


def test_symlink_helper_skips_when_windows_symlink_privilege_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def _raise_winerror(path: Path, target: Path) -> None:
        error = OSError("missing symlink privilege")
        setattr(error, "winerror", 1314)
        raise error

    monkeypatch.setattr(Path, "symlink_to", _raise_winerror)

    with pytest.raises(pytest.skip.Exception, match="1314"):
        _symlink_to_or_skip(tmp_path / "link.md", tmp_path / "target.md")


def test_renderer_prefers_absolute_local_role_prompt_override(tmp_path):
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    (prompts_dir / "shared/resume.md").write_text("absolute local resume prompt")
    renderer = PromptRenderer(_cfg_for_prompts_dir(prompts_dir))

    result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))

    assert result == "absolute local resume prompt"


def test_renderer_falls_back_per_file_for_partial_absolute_local_role_tree(tmp_path):
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "work").mkdir(parents=True)
    (prompts_dir / "work" / "behavior.md").write_text(
        "local behavior prompt {{ISSUE_NUMBER}}"
    )
    renderer = PromptRenderer(_cfg_for_prompts_dir(prompts_dir))

    behavior_result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {
                "ISSUE_NUMBER": "1",
                "ISSUE_TITLE": "title",
                "ISSUE_BODY": "",
                "ISSUE_COMMENTS": "",
                "BRANCH": "pycastle/issue-1",
                "INTERRUPTED_WORK": "",
            },
            _noop_exec,
        )
    )
    resume_result = _run(renderer.render(PromptTemplate.RESUME, {}, _noop_exec))
    shipped_resume = _run(
        PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR)).render(
            PromptTemplate.RESUME, {}, _noop_exec
        )
    )

    assert behavior_result == "local behavior prompt 1"
    assert resume_result == shipped_resume


def test_renderer_mixes_local_and_bundled_shared_prompt_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    standards_dir = tmp_path / "pycastle" / "prompts" / "shared/standards"
    standards_dir.mkdir(parents=True)
    (standards_dir / "_design.md").write_text("local design guidance")
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            _noop_exec,
        )
    )
    bundled_implementation = (
        _SHIPPED_PROMPTS_DIR / "shared/standards" / "_implementation.md"
    ).read_text(encoding="utf-8")

    assert "local design guidance" in result
    assert bundled_implementation in result


def test_render_shipped_preflight_issue_prompt():
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.PREFLIGHT_ISSUE,
            {
                "CHECK_NAME": "pytest suite",
                "COMMAND": "pytest",
                "OUTPUT": "boom",
            },
            _noop_exec,
        )
    )

    assert "CHECKS" not in result
    assert "pytest suite" in result
    assert "pytest" in result
    assert "boom" in result
    assert "{{EXPECTED_OUTPUT_SHAPE}}" not in result


def test_render_shipped_host_check_issue_prompt():
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.HOST_CHECK_ISSUE,
            {
                "HOST_OS": "Windows",
                "HOST_PLATFORM": "win32",
                "CHECKED_SHA": "abc123",
                "CHECK_NAME": "pytest host suite",
                "COMMAND": "pytest tests/host",
                "OUTPUT": "boom",
            },
            _noop_exec,
        )
    )

    assert "Windows" in result
    assert "win32" in result
    assert "abc123" in result
    assert "pytest host suite" in result
    assert "pytest tests/host" in result
    assert "boom" in result
    assert "{{EXPECTED_OUTPUT_SHAPE}}" not in result


def test_render_shipped_improve_scan_prompt_includes_recent_improve_prd_titles():
    renderer = PromptRenderer(Config())

    result = _run(
        renderer.render(
            PromptTemplate.IMPROVE_SCAN,
            {
                "RECENT_IMPROVE_PRD_TITLES": "#12 OPEN - First candidate",
            },
            _noop_exec,
        )
    )

    assert "#12 OPEN - First candidate" in result


# ── Shipped prompt contract checks now assert rendered behavior ─────────────


def test_shipped_templates_render_without_unresolved_placeholders():
    renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))
    for template in PromptTemplate:
        scope_args = {placeholder: "x" for placeholder in template.scope.placeholders}
        result = _run(renderer.render(template, scope_args, _noop_exec))
        unresolved = set(re.findall(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", result))
        assert not unresolved, (
            f"{template.filename} rendered with unresolved placeholders: {unresolved}"
        )


# ── Template shell expression tests ──────────────────────────────────────────


def test_template_shell_expr_runs_arg_shell_token_stays_inert(cfg, prompts_dir):
    (prompts_dir / "work" / "behavior.md").write_text(
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
                "INTERRUPTED_WORK": "",
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
    text = (_SHIPPED_PROMPTS_DIR / "shared/_placeholder-info.md").read_text(
        encoding="utf-8"
    )
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


def test_placeholder_info_global_tokens_match_code():
    global_tokens, _ = _parse_placeholder_info()
    expected_from_cfg = {
        spec.prompt_placeholder
        for spec in PROMPT_GLOBAL_LABEL_SPECS
        if spec.prompt_placeholder is not None
    }
    scope_tokens = {
        placeholder for scope in Scope for placeholder in scope.placeholders
    }
    shipped_tokens: set[str] = set()
    for path in _SHIPPED_PROMPTS_DIR.rglob("*.md"):
        if path.name == "_placeholder-info.md":
            continue
        shipped_tokens.update(_TOKEN_RE.findall(path.read_text(encoding="utf-8")))
    expected = (shipped_tokens - scope_tokens) | expected_from_cfg

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


# ── diagnostics/failure-report.md conditional rendering ───────────────────────────────────

_FAILURE_REPORT_SCOPE_ARGS_BASE = {
    "FAILED_ROLE": "implementer",
    "SESSION_DIR": "/sessions/abc",
}


def test_failure_report_renders_recovery_section_for_non_typed_crash():
    renderer = PromptRenderer(Config())

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
    renderer = PromptRenderer(Config())

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
    (prompts_dir / "diagnostics/failure-report.md").write_text(
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
    (prompts_dir / "diagnostics/failure-report.md").write_text(
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
    (prompts_dir / "diagnostics/failure-report.md").write_text(
        "{{#if UNKNOWN_KEY=value}}\nContent\n{{/if}}"
    )
    with pytest.raises(PromptRenderError, match="UNKNOWN_KEY"):
        PromptRenderer(cfg)


# ── INTERRUPTED_WORK scope placeholder ───────────────────────────────────────


def test_scope_per_issue_includes_interrupted_work():
    assert "INTERRUPTED_WORK" in Scope.PER_ISSUE.placeholders


def test_render_includes_interrupted_work_clause_for_fresh_dirty_worktree(
    cfg, prompts_dir
):
    (prompts_dir / "work" / "behavior.md").write_text(
        "Context:{{INTERRUPTED_WORK}}Done"
    )
    renderer = PromptRenderer(cfg)
    interrupted_work = build_interrupted_work_clause(RunKind.FRESH, is_dirty=True)

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {**_PER_ISSUE_BASE, "INTERRUPTED_WORK": interrupted_work},
            _noop_exec,
        )
    )

    assert "Interrupted Work" in result
    assert "git diff" in result
    assert "git status" in result
    assert "diff --git" not in result


_PER_ISSUE_BASE = {
    "ISSUE_NUMBER": "42",
    "ISSUE_TITLE": "Fix bug",
    "ISSUE_BODY": "",
    "ISSUE_COMMENTS": "",
    "BRANCH": "pycastle/issue-42",
}


def test_render_omits_interrupted_work_clause_when_clean(cfg, prompts_dir):
    (prompts_dir / "work" / "behavior.md").write_text(
        "Context:{{INTERRUPTED_WORK}}Done"
    )
    renderer = PromptRenderer(cfg)

    result = _run(
        renderer.render(
            PromptTemplate.IMPLEMENT_BEHAVIOR,
            {**_PER_ISSUE_BASE, "INTERRUPTED_WORK": ""},
            _noop_exec,
        )
    )

    assert "Interrupted Work" not in result
    assert result == "Context:Done"


# ── coordination/diverge.md contract ────────────────────────────────────────────────


def test_rendered_merge_prompt_includes_expected_output_shape():
    renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))
    result = _run(
        renderer.render(
            PromptTemplate.MERGE, {"BRANCHES": "branch-a\nbranch-b"}, _noop_exec
        )
    )

    assert "{{EXPECTED_OUTPUT_SHAPE}}" not in result
    assert "<commit_message>" in result


def test_rendered_diverge_prompt_has_expected_output_shape_and_no_checks_placeholder():
    renderer = PromptRenderer(_cfg_for_prompts_dir(_SHIPPED_PROMPTS_DIR))
    result = _run(
        renderer.render(
            PromptTemplate.DIVERGENCE_RESOLVE,
            {"BRANCH": "feature-branch"},
            _noop_exec,
        )
    )

    assert "{{EXPECTED_OUTPUT_SHAPE}}" not in result
    assert "{{CHECKS}}" not in result
    assert "<promise>COMPLETE</promise>" in result
