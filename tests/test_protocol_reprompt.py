from pathlib import Path
from types import SimpleNamespace

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


def _scope_args_for(template: PromptTemplate) -> dict[str, str]:
    if template in {
        PromptTemplate.IMPLEMENT_BEHAVIOR,
        PromptTemplate.IMPLEMENT_REFACTOR,
        PromptTemplate.IMPLEMENT_DOCS,
        PromptTemplate.REVIEW,
    }:
        return {
            "ISSUE_NUMBER": "1928",
            "ISSUE_TITLE": "Title",
            "ISSUE_BODY": "Body",
            "ISSUE_COMMENTS": "",
            "BRANCH": "pycastle/issue-1928",
            "INTERRUPTED_WORK": "",
        }
    if template is PromptTemplate.MERGE:
        return {"BRANCHES": "main,feature"}
    if template is PromptTemplate.PREFLIGHT_ISSUE:
        return {
            "CHECK_NAME": "lint",
            "COMMAND": "ruff check",
            "OUTPUT": "failure",
        }
    if template is PromptTemplate.HOST_CHECK_ISSUE:
        return {
            "HOST_OS": "Linux",
            "HOST_PLATFORM": "x86_64",
            "CHECKED_SHA": "abc123",
            "CHECK_NAME": "lint",
            "COMMAND": "ruff check",
            "OUTPUT": "failure",
        }
    if template in {
        PromptTemplate.IMPROVE_PRD,
        PromptTemplate.IMPROVE_NO_CANDIDATE,
    }:
        return {
            "IMPROVE_SHORT_SID": "abc123",
            "RECENT_IMPROVE_PRDS": "[]",
        }
    if template is PromptTemplate.IMPROVE_SCAN:
        return {"RECENT_IMPROVE_PRD_TITLES": "[]"}
    if template is PromptTemplate.IMPROVE_ISSUES:
        return {
            "IMPROVE_SHORT_SID": "abc123",
            "ISSUE_NUMBER": "1928",
            "ISSUE_TITLE": "Title",
            "ISSUE_BODY": "Body",
            "ISSUE_COMMENTS": "",
        }
    if template is PromptTemplate.DIVERGENCE_RESOLVE:
        return {"BRANCH": "pycastle/issue-1928"}
    if template is PromptTemplate.FAILURE_REPORT:
        return {
            "FAILED_ROLE": "reviewer",
            "SESSION_DIR": "/tmp/session",
            "EVIDENCE_PATH": "/tmp/log.txt",
            "HAS_EVIDENCE_PATH": "true",
            "FAILURE_CLASS": "HardAgentError",
        }
    if template is PromptTemplate.PLAN:
        return {
            "ALL_OPEN_ISSUES_JSON": "[]",
            "READY_FOR_AGENT_ISSUES_JSON": "[]",
        }
    if template is PromptTemplate.RESUME:
        return {}
    raise AssertionError(f"Unhandled template: {template}")


def _invocation(template: PromptTemplate):
    return build_prompt_invocation(template, _scope_args_for(template))


def test_plan_protocol_reprompt_returns_unsupported_for_resume_without_rendering():
    calls: list[object] = []

    plan = plan_protocol_reprompt(
        role=AgentRole.PLANNER,
        invocation=_invocation(PromptTemplate.RESUME),
        parser_error="missing tag",
        render_expected_output_shape=lambda _invocation: calls.append(object()) or "",
    )

    assert plan == UnsupportedProtocolReprompt()
    assert calls == []


def test_plan_protocol_reprompt_returns_planner_specific_message():
    plan = plan_protocol_reprompt(
        role=AgentRole.PLANNER,
        invocation=_invocation(PromptTemplate.PLAN),
        parser_error="invalid json",
        render_expected_output_shape=lambda _invocation: "<plan>{...}</plan>",
    )

    assert plan == TemplateSpecificProtocolReprompt(
        message="\n".join(
            [
                "Your last response did not include the required protocol output.",
                "Please review the task requirements and try again, making sure to include the required output tag.",
                "The parser reported the following error:",
                "invalid json",
                "On retry, return a raw JSON object in a `<plan>` tag (do not quote or escape the JSON).",
                "Use this Planner output shape exactly:",
                "<plan>{...}</plan>",
            ]
        )
    )


def test_plan_protocol_reprompt_calls_expected_output_shape_callback_with_original_invocation():
    scope_args = {
        "ALL_OPEN_ISSUES_JSON": '[{"number": 1, "title": "Fix A"}]',
        "READY_FOR_AGENT_ISSUES_JSON": '[{"number": 1, "title": "Fix A"}]',
    }
    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args=scope_args,
    )
    seen: list[object] = []

    plan = plan_protocol_reprompt(
        role=AgentRole.PLANNER,
        invocation=invocation,
        parser_error="invalid json",
        render_expected_output_shape=lambda received_invocation: (
            seen.append(received_invocation) or "<plan>{...}</plan>"
        ),
    )

    assert isinstance(plan, TemplateSpecificProtocolReprompt)
    assert len(seen) == 1
    assert seen[0] is invocation
    assert seen[0].template is PromptTemplate.PLAN
    assert seen[0].scope_args is scope_args


def test_plan_protocol_reprompt_returns_template_specific_message_for_host_check_issue():
    plan = plan_protocol_reprompt(
        role=AgentRole.PREFLIGHT_ISSUE,
        invocation=_invocation(PromptTemplate.HOST_CHECK_ISSUE),
        parser_error="missing issue tag",
        render_expected_output_shape=lambda _invocation: "<issue>{...}</issue>",
    )

    assert plan == TemplateSpecificProtocolReprompt(
        message="\n".join(
            [
                "Your last response did not include the required protocol output.",
                "Please review the task requirements and try again, making sure to include the required output tag.",
                "The parser reported the following error:",
                "missing issue tag",
                "Use this output shape exactly:",
                "<issue>{...}</issue>",
            ]
        )
    )


def test_plan_protocol_reprompt_returns_template_specific_message_for_all_diagnostic_templates():
    seen: list[PromptInvocation] = []
    invocations: list[PromptInvocation] = []

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
        invocation = _invocation(template)
        invocations.append(invocation)

        plan = plan_protocol_reprompt(
            role=role,
            invocation=invocation,
            parser_error="unexpected <issue> tag while ignoring <promise>COMPLETE</promise>",
            render_expected_output_shape=lambda received_invocation: (
                seen.append(received_invocation)
                or f"shape for {received_invocation.template.name}"
                f" with {expected_scope_fragment}"
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

    assert seen == invocations
    assert seen[0] is invocations[0]
    assert seen[0].scope_args is invocations[0].scope_args
    assert seen[1] is invocations[1]
    assert seen[1].scope_args is invocations[1].scope_args
    assert seen[2] is invocations[2]
    assert seen[2].scope_args is invocations[2].scope_args
    assert seen[1].scope_args["HOST_OS"] == "Linux"
    assert seen[1].scope_args["CHECKED_SHA"] == "abc123"
    assert seen[2].scope_args["FAILED_ROLE"] == "reviewer"
    assert seen[2].scope_args["FAILURE_CLASS"] == "HardAgentError"


def test_plan_protocol_reprompt_returns_coordination_template_specific_outcomes():
    seen: list[PromptInvocation] = []
    invocations: list[PromptInvocation] = []

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
        invocations.append(invocation)

        plan = plan_protocol_reprompt(
            role=role,
            invocation=invocation,
            parser_error="unexpected <promise>COMPLETE</promise> tag",
            render_expected_output_shape=lambda received_invocation: (
                seen.append(received_invocation)
                or f"shape for {received_invocation.template.name}"
                f" with {received_invocation.scope_args[scope_key]}"
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

    assert seen == invocations
    assert seen[0] is invocations[0]
    assert seen[0].scope_args is invocations[0].scope_args
    assert seen[1] is invocations[1]
    assert seen[1].scope_args is invocations[1].scope_args


def test_plan_protocol_reprompt_returns_template_specific_message_for_work_family_templates():
    render_calls: list[PromptInvocation] = []

    for role, template in (
        (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_BEHAVIOR),
        (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_REFACTOR),
        (AgentRole.IMPLEMENTER, PromptTemplate.IMPLEMENT_DOCS),
        (AgentRole.REVIEWER, PromptTemplate.REVIEW),
    ):
        invocation = _invocation(template)
        plan = plan_protocol_reprompt(
            role=role,
            invocation=invocation,
            parser_error="missing commit_message tag",
            render_expected_output_shape=lambda received_invocation: (
                (render_calls.append(received_invocation) or "")
                + (
                    f"shape for {received_invocation.template.name} "
                    f"with {received_invocation.scope_args['BRANCH']}"
                )
            ),
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

    assert render_calls == [
        _invocation(PromptTemplate.IMPLEMENT_BEHAVIOR),
        _invocation(PromptTemplate.IMPLEMENT_REFACTOR),
        _invocation(PromptTemplate.IMPLEMENT_DOCS),
        _invocation(PromptTemplate.REVIEW),
    ]


def test_plan_protocol_reprompt_returns_improve_specific_message():
    plan = plan_protocol_reprompt(
        role=AgentRole.IMPROVE,
        invocation=_invocation(PromptTemplate.IMPROVE_PRD),
        parser_error="missing promise tag",
        render_expected_output_shape=lambda _invocation: "<issue>{...}</issue>",
    )

    assert plan == TemplateSpecificProtocolReprompt(
        message="\n".join(
            [
                "Your last response did not include the required protocol output.",
                "Please review the task requirements and try again, making sure to include the required output tag.",
                "The parser reported the following error:",
                "missing promise tag",
                "Use this Improve output shape exactly:",
                "<issue>{...}</issue>",
            ]
        )
    )


def test_plan_protocol_reprompt_preserves_exact_improve_phase_invocations():
    seen: list[PromptInvocation] = []
    invocations: list[PromptInvocation] = []

    for template, expected_scope_fragment in (
        (PromptTemplate.IMPROVE_SCAN, "RECENT_IMPROVE_PRD_TITLES=[]"),
        (PromptTemplate.IMPROVE_PRD, "RECENT_IMPROVE_PRDS=[]"),
        (PromptTemplate.IMPROVE_ISSUES, "ISSUE_NUMBER=1928"),
        (PromptTemplate.IMPROVE_NO_CANDIDATE, "RECENT_IMPROVE_PRDS=[]"),
    ):
        invocation = _invocation(template)
        invocations.append(invocation)

        plan = plan_protocol_reprompt(
            role=AgentRole.IMPROVE,
            invocation=invocation,
            parser_error=(
                "unexpected <issue>123</issue> while ignoring "
                "<promise>COMPLETE</promise>"
            ),
            render_expected_output_shape=lambda received_invocation: (
                seen.append(received_invocation)
                or f"shape for {received_invocation.template.name} with "
                f"{expected_scope_fragment}"
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

    assert seen == invocations
    assert seen[0] is invocations[0]
    assert seen[0].scope_args["RECENT_IMPROVE_PRD_TITLES"] == "[]"
    assert seen[1] is invocations[1]
    assert seen[1].scope_args["RECENT_IMPROVE_PRDS"] == "[]"
    assert seen[2] is invocations[2]
    assert seen[2].scope_args["ISSUE_NUMBER"] == "1928"
    assert seen[3] is invocations[3]
    assert seen[3].scope_args["RECENT_IMPROVE_PRDS"] == "[]"


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
        render_expected_output_shape=lambda invocation: (
            renderer.render_expected_output_shape(
                invocation.template,
                invocation.scope_args,
            )
        ),
    )
    no_candidate_plan = plan_protocol_reprompt(
        role=AgentRole.IMPROVE,
        invocation=no_candidate_invocation,
        parser_error=parser_error,
        render_expected_output_shape=lambda invocation: (
            renderer.render_expected_output_shape(
                invocation.template,
                invocation.scope_args,
            )
        ),
    )

    assert issues_plan == TemplateSpecificProtocolReprompt(
        message="\n".join(
            [
                "Your last response did not include the required protocol output.",
                "Please review the task requirements and try again, making sure to include the required output tag.",
                "The parser reported the following error:",
                "unexpected <issue>17</issue> before <promise>COMPLETE</promise>",
                "Use this Improve output shape exactly:",
                issues_shape,
            ]
        )
    )
    assert "Output each filed PRD issue number as `<issue>N</issue>`." in (
        no_candidate_shape
    )
    assert no_candidate_shape != issues_shape
    assert no_candidate_plan == TemplateSpecificProtocolReprompt(
        message="\n".join(
            [
                "Your last response did not include the required protocol output.",
                "Please review the task requirements and try again, making sure to include the required output tag.",
                "The parser reported the following error:",
                "unexpected <issue>17</issue> before <promise>COMPLETE</promise>",
                "Use this Improve output shape exactly:",
                no_candidate_shape,
            ]
        )
    )
    assert no_candidate_plan != issues_plan


def test_plan_protocol_reprompt_returns_generic_fallback_without_rendering():
    calls: list[object] = []

    plan = plan_protocol_reprompt(
        role=AgentRole.IMPLEMENTER,
        invocation=_invocation(PromptTemplate.PLAN),
        parser_error="missing tag",
        render_expected_output_shape=lambda _invocation: calls.append(object()) or "",
    )

    assert plan == GenericProtocolReprompt(message=GENERIC_PROTOCOL_REPROMPT_MESSAGE)
    assert calls == []
