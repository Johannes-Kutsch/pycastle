from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.protocol_reprompt import (
    GENERIC_PROTOCOL_REPROMPT_MESSAGE,
    GenericProtocolReprompt,
    TemplateSpecificProtocolReprompt,
    UnsupportedProtocolReprompt,
    plan_protocol_reprompt,
)
from pycastle.prompts.dispatch import PromptInvocation, build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate


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


def test_plan_protocol_reprompt_returns_template_specific_message_for_work_family_templates():
    for template in (
        PromptTemplate.IMPLEMENT_BEHAVIOR,
        PromptTemplate.IMPLEMENT_REFACTOR,
        PromptTemplate.IMPLEMENT_DOCS,
        PromptTemplate.REVIEW,
    ):
        invocation = _invocation(template)
        plan = plan_protocol_reprompt(
            role=AgentRole.IMPLEMENTER,
            invocation=invocation,
            parser_error="missing commit_message tag",
            render_expected_output_shape=lambda received_invocation: (
                f"shape for {received_invocation.template.name} "
                f"with {received_invocation.scope_args['BRANCH']}"
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
