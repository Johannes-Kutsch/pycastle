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
class ImprovePreparationStepConfig:
    """Per-template configuration for a step passed into prepare_improve_step."""

    template: PromptTemplate
    namespace: str
    display_name: str


@dataclass(frozen=True)
class ImprovePreparationStep:
    """A driver-produced step ready to be prepared for agent dispatch."""

    cfg: ImprovePreparationStepConfig
    kind: PromptKind
    fetch_recent_spec_titles: bool
    work_body: str
    candidate: ImproveCandidate | None = None


@dataclass(frozen=True)
class PreparedImproveStep:
    prompt: PromptInvocation
    session_namespace: str
    name: str
    work_body: str


def _compute_work_body(
    template: PromptTemplate,
    *,
    candidate_budget: int | None = None,
    candidate: ImproveCandidate | None = None,
    candidate_ordinal: int | None = None,
    scan_set_size: int | None = None,
    display_body: str = "",
) -> str:
    """Compute the human-readable work-body line for a step row."""
    if template is PromptTemplate.IMPROVE_SCAN:
        budget = candidate_budget or 0
        return (
            "picking 1 improvement"
            if budget == 1
            else f"picking up to {budget} improvements"
        )
    if (
        candidate is not None
        and candidate_ordinal is not None
        and scan_set_size is not None
    ):
        if template is PromptTemplate.IMPROVE_SPEC:
            return f'writing spec for candidate {candidate_ordinal}/{scan_set_size} "{candidate.title}"'
        if template is PromptTemplate.IMPROVE_TICKETS:
            return f'filing tickets for candidate {candidate_ordinal}/{scan_set_size} "{candidate.title}"'
    return display_body


def prepare_improve_step(
    step: ImprovePreparationStep,
    *,
    github_port: ImprovePreparationGithubPort,
    short_sid: str,
    candidate_budget: int | None = None,
) -> PreparedImproveStep:
    """Prepare the exact `RunRequest` payload for one Improve step.

    GitHub reads needed for scope args are performed through `github_port`, and
    any read error is allowed to propagate to the caller unchanged.
    """

    scope_args = _render_scope_args(
        step,
        github_port=github_port,
        short_sid=short_sid,
        candidate_budget=candidate_budget,
    )
    return PreparedImproveStep(
        prompt=build_prompt_invocation(
            step.cfg.template,
            scope_args,
            kind=step.kind,
        ),
        session_namespace=step.cfg.namespace,
        name=step.cfg.display_name,
        work_body=step.work_body,
    )


def _render_scope_args(
    step: ImprovePreparationStep,
    *,
    github_port: ImprovePreparationGithubPort,
    short_sid: str,
    candidate_budget: int | None,
) -> dict[str, str]:
    match step.cfg.template:
        case PromptTemplate.IMPROVE_SCAN:
            recent_specs = (
                github_port.get_recent_improve_specs()
                if step.fetch_recent_spec_titles
                else []
            )
            if candidate_budget is None:
                raise PromptRenderError(
                    "candidate_budget is required to render the improve scan prompt"
                )
            return build_improve_scan_scope_args(
                recent_specs=recent_specs,
                candidate_budget=candidate_budget,
            )
        case PromptTemplate.IMPROVE_SPEC:
            if step.candidate is None:
                raise PromptRenderError(
                    "candidate is required to render the spec prompt"
                )
            return validated_scope_args_for_template(
                step.cfg.template,
                {
                    "IMPROVE_SHORT_SID": short_sid,
                    "RECENT_IMPROVE_SPECS": _format_recent_improve_specs(
                        github_port.get_recent_improve_specs()
                    ),
                    "CANDIDATE_RANK": str(step.candidate.rank),
                    "CANDIDATE_TITLE": step.candidate.title,
                },
            )
        case PromptTemplate.IMPROVE_NO_CANDIDATE:
            return validated_scope_args_for_template(
                step.cfg.template,
                {
                    "IMPROVE_SHORT_SID": short_sid,
                    "RECENT_IMPROVE_SPECS": _format_recent_improve_specs(
                        github_port.get_recent_improve_specs()
                    ),
                    "CANDIDATE_RANK": "",
                    "CANDIDATE_TITLE": "",
                },
            )
        case PromptTemplate.IMPROVE_TICKETS:
            return validated_scope_args_for_template(
                step.cfg.template,
                {"IMPROVE_SHORT_SID": short_sid},
            )
        case _:
            raise TypeError(f"unsupported Improve template: {step.cfg.template.name}")


def _format_recent_improve_specs(recent_specs: list[dict[str, Any]]) -> str:
    if not recent_specs:
        return "No recent improve specs found."
    return "\n".join(
        f"#{spec['number']} {spec['state']} - {spec['title']}" for spec in recent_specs
    )
