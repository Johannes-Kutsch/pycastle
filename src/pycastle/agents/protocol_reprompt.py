from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from ..prompts.dispatch import PromptInvocation
from ..prompts.pipeline import PromptTemplate
from .output_protocol import AgentRole

GENERIC_PROTOCOL_REPROMPT_MESSAGE = (
    "Your last response did not include the required protocol output. "
    "Please review the task requirements and try again, making sure to "
    "include the required output tag."
)

ExpectedOutputShapeRenderer: TypeAlias = Callable[[PromptInvocation], str]


@dataclass(frozen=True)
class UnsupportedProtocolReprompt:
    kind: Literal["unsupported"] = "unsupported"


@dataclass(frozen=True)
class GenericProtocolReprompt:
    message: str = GENERIC_PROTOCOL_REPROMPT_MESSAGE
    kind: Literal["generic"] = "generic"


@dataclass(frozen=True)
class TemplateSpecificProtocolReprompt:
    message: str
    kind: Literal["template_specific"] = "template_specific"


ProtocolRepromptPlan: TypeAlias = (
    UnsupportedProtocolReprompt
    | GenericProtocolReprompt
    | TemplateSpecificProtocolReprompt
)

_HOST_PARSED_PROTOCOL_TEMPLATES = frozenset(
    {
        PromptTemplate.IMPLEMENT_BEHAVIOR,
        PromptTemplate.IMPLEMENT_REFACTOR,
        PromptTemplate.IMPLEMENT_DOCS,
        PromptTemplate.REVIEW,
        PromptTemplate.MERGE,
        PromptTemplate.PREFLIGHT_ISSUE,
        PromptTemplate.FAILURE_REPORT,
        PromptTemplate.DIVERGENCE_RESOLVE,
        PromptTemplate.HOST_CHECK_ISSUE,
    }
)
_IMPROVE_PROTOCOL_TEMPLATES = frozenset(
    {
        PromptTemplate.IMPROVE_SCAN,
        PromptTemplate.IMPROVE_PRD,
        PromptTemplate.IMPROVE_ISSUES,
        PromptTemplate.IMPROVE_NO_CANDIDATE,
    }
)


def _protocol_reprompt_message_with_expected_shape(
    *,
    parser_error: str,
    expected_shape: str,
    retry_instruction: str | None = None,
    shape_label: str = "Use this output shape exactly:",
) -> str:
    lines = [
        "Your last response did not include the required protocol output.",
        "Please review the task requirements and try again, making sure to include the required output tag.",
        "The parser reported the following error:",
        parser_error,
    ]
    if retry_instruction is not None:
        lines.append(retry_instruction)
    lines.extend([shape_label, expected_shape])
    return "\n".join(lines)


def plan_protocol_reprompt(
    *,
    role: AgentRole,
    invocation: PromptInvocation,
    parser_error: str,
    render_expected_output_shape: ExpectedOutputShapeRenderer,
) -> ProtocolRepromptPlan:
    if invocation.template is PromptTemplate.RESUME:
        return UnsupportedProtocolReprompt()

    if role is AgentRole.PLANNER:
        return TemplateSpecificProtocolReprompt(
            message=_protocol_reprompt_message_with_expected_shape(
                parser_error=parser_error,
                expected_shape=render_expected_output_shape(invocation),
                retry_instruction=(
                    "On retry, return a raw JSON object in a `<plan>` tag "
                    "(do not quote or escape the JSON)."
                ),
                shape_label="Use this Planner output shape exactly:",
            )
        )

    if invocation.template in _HOST_PARSED_PROTOCOL_TEMPLATES:
        return TemplateSpecificProtocolReprompt(
            message=_protocol_reprompt_message_with_expected_shape(
                parser_error=parser_error,
                expected_shape=render_expected_output_shape(invocation),
            )
        )

    if invocation.template in _IMPROVE_PROTOCOL_TEMPLATES:
        return TemplateSpecificProtocolReprompt(
            message=_protocol_reprompt_message_with_expected_shape(
                parser_error=parser_error,
                expected_shape=render_expected_output_shape(invocation),
                shape_label="Use this Improve output shape exactly:",
            )
        )

    return GenericProtocolReprompt()


__all__ = [
    "ExpectedOutputShapeRenderer",
    "GENERIC_PROTOCOL_REPROMPT_MESSAGE",
    "GenericProtocolReprompt",
    "ProtocolRepromptPlan",
    "TemplateSpecificProtocolReprompt",
    "UnsupportedProtocolReprompt",
    "plan_protocol_reprompt",
]
