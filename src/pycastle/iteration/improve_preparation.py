from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pycastle.prompts.dispatch import (
    PromptInvocation,
    PromptKind,
    build_prompt_invocation,
)
from pycastle.prompts.pipeline import PromptRenderError, PromptTemplate
from pycastle.prompts.scope_args import (
    build_improve_scan_scope_args,
    validated_scope_args_for_template,
)


@dataclass(frozen=True)
class ImproveCandidate:
    """Identity of a single improve candidate being worked."""

    rank: int
    title: str
    spec_number: int | None = None


class ImprovePreparationGithubPort(Protocol):
    """GitHub read contract for preparing Improve steps.

    Implementations must supply the narrow Improve reads this module needs:
    recent Improve specs, a spec issue fetch, and spec comments. Read failures
    are not translated here; callers should expect the underlying GitHub read
    exception to propagate unchanged.
    """

    def get_recent_improve_specs(self) -> list[dict[str, Any]]: ...

    def get_issue(self, issue_number: int) -> dict[str, Any]: ...

    def get_issue_comments(self, issue_number: int) -> list[dict[str, str]]: ...


@dataclass(frozen=True)
class ImproveStepPreparationRequest:
    """Inputs required to prepare a single Improve step.

    `short_sid` is required for session-scoped placeholders.
    `fetch_recent_spec_titles` preserves the existing scan-step retry behavior
    that skips the GitHub read. `candidate_budget` is required when preparing
    `PromptTemplate.IMPROVE_SCAN`.
    """

    prompt_template: PromptTemplate
    session_namespace: str
    display_name: str
    work_body: str
    kind: PromptKind
    short_sid: str
    fetch_recent_spec_titles: bool = False
    candidate_budget: int | None = None
    candidate: ImproveCandidate | None = None


@dataclass(frozen=True)
class PreparedImproveStep:
    prompt: PromptInvocation
    session_namespace: str
    name: str
    work_body: str


def prepare_improve_step(
    request: ImproveStepPreparationRequest,
    *,
    github_port: ImprovePreparationGithubPort,
) -> PreparedImproveStep:
    """Prepare the exact `RunRequest` payload for one Improve step.

    GitHub reads needed for scope args are performed through `github_port`, and
    any read error is allowed to propagate to the caller unchanged.
    """

    scope_args = _render_scope_args(request, github_port=github_port)
    return PreparedImproveStep(
        prompt=build_prompt_invocation(
            request.prompt_template,
            scope_args,
            kind=request.kind,
        ),
        session_namespace=request.session_namespace,
        name=request.display_name,
        work_body=request.work_body,
    )


def _render_scope_args(
    request: ImproveStepPreparationRequest,
    *,
    github_port: ImprovePreparationGithubPort,
) -> dict[str, str]:
    match request.prompt_template:
        case PromptTemplate.IMPROVE_SCAN:
            recent_specs = (
                github_port.get_recent_improve_specs()
                if request.fetch_recent_spec_titles
                else []
            )
            if request.candidate_budget is None:
                raise PromptRenderError(
                    "candidate_budget is required to render the improve scan prompt"
                )
            return build_improve_scan_scope_args(
                recent_specs=recent_specs,
                candidate_budget=request.candidate_budget,
            )
        case PromptTemplate.IMPROVE_SPEC:
            if request.candidate is None:
                raise PromptRenderError(
                    "candidate is required to render the spec prompt"
                )
            return validated_scope_args_for_template(
                request.prompt_template,
                {
                    "IMPROVE_SHORT_SID": request.short_sid,
                    "RECENT_IMPROVE_SPECS": _format_recent_improve_specs(
                        github_port.get_recent_improve_specs()
                    ),
                    "CANDIDATE_RANK": str(request.candidate.rank),
                    "CANDIDATE_TITLE": request.candidate.title,
                },
            )
        case PromptTemplate.IMPROVE_NO_CANDIDATE:
            return validated_scope_args_for_template(
                request.prompt_template,
                {
                    "IMPROVE_SHORT_SID": request.short_sid,
                    "RECENT_IMPROVE_SPECS": _format_recent_improve_specs(
                        github_port.get_recent_improve_specs()
                    ),
                    "CANDIDATE_RANK": "",
                    "CANDIDATE_TITLE": "",
                },
            )
        case PromptTemplate.IMPROVE_TICKETS:
            return validated_scope_args_for_template(
                request.prompt_template,
                {"IMPROVE_SHORT_SID": request.short_sid},
            )
        case _:
            raise TypeError(
                f"unsupported Improve template: {request.prompt_template.name}"
            )


def _format_recent_improve_specs(recent_specs: list[dict[str, Any]]) -> str:
    if not recent_specs:
        return "No recent improve specs found."
    return "\n".join(
        f"#{spec['number']} {spec['state']} - {spec['title']}" for spec in recent_specs
    )
