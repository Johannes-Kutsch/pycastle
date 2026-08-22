from pathlib import Path
from types import SimpleNamespace

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.protocol_reprompt import (
    GENERIC_PROTOCOL_REPROMPT_MESSAGE,
    GenericProtocolReprompt,
    TemplateSpecificProtocolReprompt,
    UnsupportedProtocolReprompt,
    plan_protocol_reprompt,
)
from pycastle.prompts.dispatch import PromptInvocation, build_prompt_invocation
from pycastle.prompts.pipeline import PromptRenderer, PromptTemplate

_SHIPPED_PROMPTS_DIR = (
    Path(__file__).parent.parent / "src" / "pycastle" / "defaults" / "prompts"
)


def _renderer() -> PromptRenderer:
    return PromptRenderer(
        SimpleNamespace(
            prompts_dir=_SHIPPED_PROMPTS_DIR,
            preflight_checks=(("pytest suite", "pytest"),),
            bug_label="bug",
            issue_label="ready-for-agent",
            hitl_label="human-in-the-loop",
            enhancement_label="enhancement",
            needs_triage_label="needs-triage",
            needs_info_label="needs-info",
            wontfix_label="wontfix",
            refactor_slice_label="refactor-slice",
            behavior_slice_label="behavior-slice",
            docs_slice_label="docs-slice",
            implement_checks=("ruff check --fix", "ruff format --check", "mypy ."),
        )
    )


_ISSUE_SCOPE_ARGS: dict[str, str] = {
    "ISSUE_NUMBER": "1928",
    "ISSUE_TITLE": "Title",
    "ISSUE_BODY": "Body",
    "ISSUE_COMMENTS": "",
    "BRANCH": "pycastle/issue-1928",
    "INTERRUPTED_WORK": "",
    "OPERATING_BRANCH": "main",
}

_IMPROVE_PRD_SCOPE_ARGS: dict[str, str] = {
    "IMPROVE_SHORT_SID": "abc123",
    "RECENT_IMPROVE_PRDS": "[]",
    "CANDIDATE_RANK": "1",
    "CANDIDATE_TITLE": "Deepen the parser module",
}

_SCOPE_ARGS_BY_TEMPLATE: dict[PromptTemplate, dict[str, str]] = {
    PromptTemplate.IMPLEMENT_BEHAVIOR: _ISSUE_SCOPE_ARGS,
    PromptTemplate.IMPLEMENT_REFACTOR: _ISSUE_SCOPE_ARGS,
    PromptTemplate.IMPLEMENT_DOCS: _ISSUE_SCOPE_ARGS,
    PromptTemplate.REVIEW: _ISSUE_SCOPE_ARGS,
    PromptTemplate.MERGE: {"BRANCHES": "main,feature"},
    PromptTemplate.PREFLIGHT_ISSUE: {
        "CHECK_NAME": "lint",
        "COMMAND": "ruff check",
        "OUTPUT": "failure",
    },
    PromptTemplate.HOST_CHECK_ISSUE: {
        "HOST_OS": "Linux",
        "HOST_PLATFORM": "x86_64",
        "CHECKED_SHA": "abc123",
        "CHECK_NAME": "lint",
        "COMMAND": "ruff check",
        "OUTPUT": "failure",
    },
    PromptTemplate.IMPROVE_PRD: _IMPROVE_PRD_SCOPE_ARGS,
    PromptTemplate.IMPROVE_NO_CANDIDATE: _IMPROVE_PRD_SCOPE_ARGS,
    PromptTemplate.IMPROVE_SCAN: {
        "RECENT_IMPROVE_PRD_TITLES": "[]",
        "CANDIDATE_BUDGET": "3",
    },
    PromptTemplate.IMPROVE_ISSUES: {
        "IMPROVE_SHORT_SID": "abc123",
    },
    PromptTemplate.IMPROVE_DRAFT_CORRECTION: {
        "VALIDATION_ERRORS": "- Error 1",
    },
    PromptTemplate.DIVERGENCE_RESOLVE: {"BRANCH": "pycastle/issue-1928"},
    PromptTemplate.FAILURE_REPORT: {
        "FAILED_ROLE": "reviewer",
        "SESSION_DIR": "/tmp/session",
        "EVIDENCE_PATH": "/tmp/log.txt",
        "HAS_EVIDENCE_PATH": "true",
        "FAILURE_CLASS": "HardAgentError",
    },
    PromptTemplate.PLAN: {
        "ALL_OPEN_ISSUES_JSON": "[]",
        "READY_FOR_AGENT_ISSUES_JSON": "[]",
    },
    PromptTemplate.RESUME: {},
}


def _scope_args_for(template: PromptTemplate) -> dict[str, str]:
    try:
        return _SCOPE_ARGS_BY_TEMPLATE[template]
    except KeyError:
        raise AssertionError(f"Unhandled template: {template}") from None


def _invocation(template: PromptTemplate):
    return build_prompt_invocation(template, _scope_args_for(template))


_TEMPLATE_SPECIFIC_PROTOCOL_CASES = (
    (AgentRole.PLANNER, PromptTemplate.PLAN),
    (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_BEHAVIOR),
    (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_REFACTOR),
    (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_DOCS),
    (AgentRole.REVIEWER, PromptTemplate.REVIEW),
    (AgentRole.PREFLIGHT_ISSUE, PromptTemplate.PREFLIGHT_ISSUE),
    (AgentRole.PREFLIGHT_ISSUE, PromptTemplate.HOST_CHECK_ISSUE),
    (AgentRole.MERGER, PromptTemplate.MERGE),
    (AgentRole.IMPROVE, PromptTemplate.IMPROVE_SCAN),
    (AgentRole.IMPROVE, PromptTemplate.IMPROVE_PRD),
    (AgentRole.IMPROVE, PromptTemplate.IMPROVE_ISSUES),
    (AgentRole.IMPROVE, PromptTemplate.IMPROVE_NO_CANDIDATE),
    (AgentRole.IMPROVE, PromptTemplate.IMPROVE_DRAFT_CORRECTION),
    (AgentRole.FAILURE_REPORT, PromptTemplate.FAILURE_REPORT),
    (AgentRole.DIVERGENCE_RESOLVER, PromptTemplate.DIVERGENCE_RESOLVE),
)


def test_plan_protocol_reprompt_returns_unsupported_for_resume_without_rendering():
    calls: list[object] = []

    def render_expected_output_shape() -> str:
        calls.append(object())
        return ""

    plan = plan_protocol_reprompt(
        role=AgentRole.PLANNER,
        invocation=_invocation(PromptTemplate.RESUME),
        parser_error="missing tag",
        render_expected_output_shape=render_expected_output_shape,
    )

    assert plan == UnsupportedProtocolReprompt()
    assert calls == []


@pytest.mark.parametrize("role", tuple(AgentRole))
def test_plan_protocol_reprompt_returns_unsupported_for_resume_for_every_role(
    role: AgentRole,
):
    calls: list[object] = []

    def render_expected_output_shape() -> str:
        calls.append(object())
        return ""

    plan = plan_protocol_reprompt(
        role=role,
        invocation=_invocation(PromptTemplate.RESUME),
        parser_error="missing tag",
        render_expected_output_shape=render_expected_output_shape,
    )

    assert plan == UnsupportedProtocolReprompt()
    assert calls == []


def test_plan_protocol_reprompt_returns_planner_specific_message():
    plan = plan_protocol_reprompt(
        role=AgentRole.PLANNER,
        invocation=_invocation(PromptTemplate.PLAN),
        parser_error="invalid json",
        render_expected_output_shape=lambda: "<plan>{...}</plan>",
    )

    assert plan == TemplateSpecificProtocolReprompt(
        message=(
            "Your last response did not include the required protocol output.\n"
            "Please review the task requirements and try again, making sure to include the required output tag.\n"
            "The parser reported the following error:\n"
            "invalid json\n"
            "On retry, return a raw JSON object in a `<plan>` tag (do not quote or escape the JSON).\n"
            "Use this Planner output shape exactly:\n"
            "<plan>{...}</plan>"
        )
    )


def test_plan_protocol_reprompt_calls_expected_output_shape_callback_once():
    seen: list[object] = []

    def render_expected_output_shape() -> str:
        seen.append(object())
        return "<plan>{...}</plan>"

    plan = plan_protocol_reprompt(
        role=AgentRole.PLANNER,
        invocation=_invocation(PromptTemplate.PLAN),
        parser_error="invalid json",
        render_expected_output_shape=render_expected_output_shape,
    )

    assert isinstance(plan, TemplateSpecificProtocolReprompt)
    assert len(seen) == 1


def test_plan_protocol_reprompt_returns_template_specific_message_for_host_check_issue():
    plan = plan_protocol_reprompt(
        role=AgentRole.PREFLIGHT_ISSUE,
        invocation=_invocation(PromptTemplate.HOST_CHECK_ISSUE),
        parser_error="missing issue tag",
        render_expected_output_shape=lambda: "<issue>{...}</issue>",
    )

    assert plan == TemplateSpecificProtocolReprompt(
        message=(
            "Your last response did not include the required protocol output.\n"
            "Please review the task requirements and try again, making sure to include the required output tag.\n"
            "The parser reported the following error:\n"
            "missing issue tag\n"
            "Use this output shape exactly:\n"
            "<issue>{...}</issue>"
        )
    )


def test_plan_protocol_reprompt_returns_template_specific_message_for_all_diagnostic_templates():
    for role, template, expected_scope_fragment in (
        (
            AgentRole.PREFLIGHT_ISSUE,
            PromptTemplate.PREFLIGHT_ISSUE,
            "ruff check",
        ),
        (
            AgentRole.PREFLIGHT_ISSUE,
            PromptTemplate.HOST_CHECK_ISSUE,
            "abc123",
        ),
        (
            AgentRole.FAILURE_REPORT,
            PromptTemplate.FAILURE_REPORT,
            "protocol_error",
        ),
    ):
        plan = plan_protocol_reprompt(
            role=role,
            invocation=_invocation(template),
            parser_error="unexpected <issue> tag while ignoring <promise>COMPLETE</promise>",
            render_expected_output_shape=lambda t=template, esf=expected_scope_fragment: (
                f"shape for {t.name} with {esf}"
            ),
        )

        assert plan == TemplateSpecificProtocolReprompt(
            message="\n".join(
                [
                    "Your last response did not include the required protocol output.",
                    "Please review the task requirements and try again, making sure to include the required output tag.",
                    "The parser reported the following error:",
                    "unexpected <issue> tag while ignoring <promise>COMPLETE</promise>",
                    "Use this output shape exactly:",
                    f"shape for {template.name} with {expected_scope_fragment}",
                ]
            )
        )


def test_plan_protocol_reprompt_returns_coordination_template_specific_outcomes():
    for role, template, scope_key, scope_value in (
        (AgentRole.MERGER, PromptTemplate.MERGE, "BRANCHES", "main,feature"),
        (
            AgentRole.DIVERGENCE_RESOLVER,
            PromptTemplate.DIVERGENCE_RESOLVE,
            "BRANCH",
            "pycastle/issue-1928",
        ),
    ):
        invocation = PromptInvocation(
            template=template,
            scope_args=_scope_args_for(template),
            send_role_prompt_on_resume=True,
        )

        plan = plan_protocol_reprompt(
            role=role,
            invocation=invocation,
            parser_error="unexpected <promise>COMPLETE</promise> tag",
            render_expected_output_shape=lambda t=template, inv=invocation, sk=scope_key: (
                f"shape for {t.name} with {inv.scope_args[sk]}"
            ),
        )

        assert isinstance(plan, TemplateSpecificProtocolReprompt)
        assert plan.message == "\n".join(
            [
                "Your last response did not include the required protocol output.",
                "Please review the task requirements and try again, making sure to include the required output tag.",
                "The parser reported the following error:",
                "unexpected <promise>COMPLETE</promise> tag",
                "Use this output shape exactly:",
                f"shape for {template.name} with {scope_value}",
            ]
        )


def test_plan_protocol_reprompt_returns_template_specific_message_for_work_family_templates():
    render_calls: list[object] = []

    for role, template in (
        (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_BEHAVIOR),
        (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_REFACTOR),
        (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_DOCS),
        (AgentRole.REVIEWER, PromptTemplate.REVIEW),
    ):
        invocation = _invocation(template)

        def render_expected_output_shape(t=template, inv=invocation) -> str:
            render_calls.append(object())
            return f"shape for {t.name} with {inv.scope_args['BRANCH']}"

        plan = plan_protocol_reprompt(
            role=role,
            invocation=invocation,
            parser_error="missing commit_message tag",
            render_expected_output_shape=render_expected_output_shape,
        )

        assert plan == TemplateSpecificProtocolReprompt(
            message="\n".join(
                [
                    "Your last response did not include the required protocol output.",
                    "Please review the task requirements and try again, making sure to include the required output tag.",
                    "The parser reported the following error:",
                    "missing commit_message tag",
                    "Use this output shape exactly:",
                    f"shape for {template.name} with pycastle/issue-1928",
                ]
            )
        )

    assert len(render_calls) == 4


def test_plan_protocol_reprompt_returns_improve_specific_message():
    plan = plan_protocol_reprompt(
        role=AgentRole.IMPROVE,
        invocation=_invocation(PromptTemplate.IMPROVE_PRD),
        parser_error="missing promise tag",
        render_expected_output_shape=lambda: "<issue>{...}</issue>",
    )

    assert plan == TemplateSpecificProtocolReprompt(
        message=(
            "Your last response did not include the required protocol output.\n"
            "Please review the task requirements and try again, making sure to include the required output tag.\n"
            "The parser reported the following error:\n"
            "missing promise tag\n"
            "Use this Improve output shape exactly:\n"
            "<issue>{...}</issue>"
        )
    )


def test_plan_protocol_reprompt_preserves_exact_improve_phase_invocations():
    for template, expected_scope_fragment in (
        (PromptTemplate.IMPROVE_SCAN, "RECENT_IMPROVE_PRD_TITLES=[]"),
        (PromptTemplate.IMPROVE_PRD, "RECENT_IMPROVE_PRDS=[]"),
        (PromptTemplate.IMPROVE_ISSUES, "IMPROVE_SHORT_SID=abc123"),
        (PromptTemplate.IMPROVE_NO_CANDIDATE, "RECENT_IMPROVE_PRDS=[]"),
    ):
        plan = plan_protocol_reprompt(
            role=AgentRole.IMPROVE,
            invocation=_invocation(template),
            parser_error=(
                "unexpected <issue>123</issue> while ignoring "
                "<promise>COMPLETE</promise>"
            ),
            render_expected_output_shape=lambda t=template, esf=expected_scope_fragment: (
                f"shape for {t.name} with {esf}"
            ),
        )

        assert plan == TemplateSpecificProtocolReprompt(
            message="\n".join(
                [
                    "Your last response did not include the required protocol output.",
                    "Please review the task requirements and try again, making sure to include the required output tag.",
                    "The parser reported the following error:",
                    "unexpected <issue>123</issue> while ignoring <promise>COMPLETE</promise>",
                    "Use this Improve output shape exactly:",
                    f"shape for {template.name} with {expected_scope_fragment}",
                ]
            )
        )


def test_plan_protocol_reprompt_uses_distinct_no_candidate_shape():
    renderer = _renderer()
    issues_invocation = _invocation(PromptTemplate.IMPROVE_ISSUES)
    no_candidate_invocation = _invocation(PromptTemplate.IMPROVE_NO_CANDIDATE)
    parser_error = "unexpected <issue>17</issue> before <promise>COMPLETE</promise>"
    issues_shape = renderer.render_expected_output_shape(
        issues_invocation.template,
        issues_invocation.scope_args,
    )
    no_candidate_shape = renderer.render_expected_output_shape(
        no_candidate_invocation.template,
        no_candidate_invocation.scope_args,
    )

    issues_plan = plan_protocol_reprompt(
        role=AgentRole.IMPROVE,
        invocation=issues_invocation,
        parser_error=parser_error,
        render_expected_output_shape=lambda: renderer.render_expected_output_shape(
            issues_invocation.template,
            issues_invocation.scope_args,
        ),
    )
    no_candidate_plan = plan_protocol_reprompt(
        role=AgentRole.IMPROVE,
        invocation=no_candidate_invocation,
        parser_error=parser_error,
        render_expected_output_shape=lambda: renderer.render_expected_output_shape(
            no_candidate_invocation.template,
            no_candidate_invocation.scope_args,
        ),
    )

    assert issues_plan == TemplateSpecificProtocolReprompt(
        message=(
            "Your last response did not include the required protocol output.\n"
            "Please review the task requirements and try again, making sure to include the required output tag.\n"
            "The parser reported the following error:\n"
            "unexpected <issue>17</issue> before <promise>COMPLETE</promise>\n"
            f"Use this Improve output shape exactly:\n{issues_shape}"
        )
    )
    assert no_candidate_shape != issues_shape
    assert no_candidate_plan == TemplateSpecificProtocolReprompt(
        message=(
            "Your last response did not include the required protocol output.\n"
            "Please review the task requirements and try again, making sure to include the required output tag.\n"
            "The parser reported the following error:\n"
            "unexpected <issue>17</issue> before <promise>COMPLETE</promise>\n"
            f"Use this Improve output shape exactly:\n{no_candidate_shape}"
        )
    )
    assert no_candidate_plan != issues_plan


def test_plan_protocol_reprompt_returns_generic_fallback_without_rendering():
    calls: list[object] = []

    def render_expected_output_shape() -> str:
        calls.append(object())
        return ""

    plan = plan_protocol_reprompt(
        role=AgentRole.IMPLEMENTER,
        invocation=_invocation(PromptTemplate.PLAN),
        parser_error="missing tag",
        render_expected_output_shape=render_expected_output_shape,
    )

    assert plan == GenericProtocolReprompt(message=GENERIC_PROTOCOL_REPROMPT_MESSAGE)
    assert calls == []


def test_plan_protocol_reprompt_returns_generic_fallback_for_mismatched_role_policy():
    calls: list[object] = []

    def render_expected_output_shape() -> str:
        calls.append(object())
        return ""

    plan = plan_protocol_reprompt(
        role=AgentRole.PLANNER,
        invocation=_invocation(PromptTemplate.MERGE),
        parser_error="missing plan tag",
        render_expected_output_shape=render_expected_output_shape,
    )

    assert plan == GenericProtocolReprompt(message=GENERIC_PROTOCOL_REPROMPT_MESSAGE)
    assert calls == []


@pytest.mark.parametrize(("role", "template"), _TEMPLATE_SPECIFIC_PROTOCOL_CASES)
def test_plan_protocol_reprompt_returns_template_specific_only_for_supported_role_template_pairs(
    role: AgentRole, template: PromptTemplate
):
    calls: list[object] = []

    def render_expected_output_shape() -> str:
        calls.append(object())
        return "<shape/>"

    plan = plan_protocol_reprompt(
        role=role,
        invocation=_invocation(template),
        parser_error="missing tag",
        render_expected_output_shape=render_expected_output_shape,
    )

    assert isinstance(plan, TemplateSpecificProtocolReprompt)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("role", "template"),
    tuple(
        (role, template)
        for role in AgentRole
        for template in PromptTemplate
        if template is not PromptTemplate.RESUME
        and (role, template) not in _TEMPLATE_SPECIFIC_PROTOCOL_CASES
    ),
)
def test_plan_protocol_reprompt_returns_generic_fallback_for_every_non_policy_template(
    role: AgentRole, template: PromptTemplate
):
    calls: list[object] = []

    def render_expected_output_shape() -> str:
        calls.append(object())
        return ""

    plan = plan_protocol_reprompt(
        role=role,
        invocation=_invocation(template),
        parser_error="missing tag",
        render_expected_output_shape=render_expected_output_shape,
    )

    assert plan == GenericProtocolReprompt(message=GENERIC_PROTOCOL_REPROMPT_MESSAGE)
    assert calls == []
