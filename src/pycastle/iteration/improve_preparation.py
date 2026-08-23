from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from pycastle.prompts.dispatch import (
    PromptInvocation,
    PromptKind,
    build_prompt_invocation,
)
from pycastle.prompts.pipeline import PromptRenderError, PromptTemplate, Scope
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
    recent Improve PRDs, a PRD issue fetch, and PRD comments. Read failures are
    not translated here; callers should expect the underlying GitHub read
    exception to propagate unchanged.
    """

    def get_recent_improve_prds(self) -> list[dict[str, Any]]: ...

    def get_issue(self, issue_number: int) -> dict[str, Any]: ...

    def get_issue_comments(self, issue_number: int) -> list[dict[str, str]]: ...


class ImprovePreparationStepConfig(Protocol):
    @property
    def template(self) -> PromptTemplate: ...

    @property
    def namespace(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def display_body(self) -> str: ...


class ImprovePreparationStep(Protocol):
    @property
    def cfg(self) -> ImprovePreparationStepConfig: ...

    @property
    def kind(self) -> PromptKind: ...

    @property
    def fetch_recent_prd_titles(self) -> bool: ...

    @property
    def candidate(self) -> ImproveCandidate | None: ...

    @property
    def scan_set_size(self) -> int | None: ...

    @property
    def candidate_ordinal(self) -> int | None: ...


@dataclass(frozen=True)
class ImproveStepPreparationRequest:
    """Inputs required to prepare a single Improve step.

    `short_sid` is required for session-scoped placeholders.
    `fetch_recent_prd_titles` preserves the existing scan-step retry behavior
    that skips the GitHub read. `candidate_budget` is required when preparing
    `PromptTemplate.IMPROVE_SCAN`.
    """

    prompt_template: PromptTemplate
    session_namespace: str
    display_name: str
    work_body: str
    kind: PromptKind
    short_sid: str
    fetch_recent_prd_titles: bool = False
    candidate_budget: int | None = None
    candidate: ImproveCandidate | None = None


@dataclass(frozen=True)
class PreparedImproveStep:
    prompt: PromptInvocation
    session_namespace: str
    name: str
    work_body: str


def prepare_improve_step(
    request_or_step: ImproveStepPreparationRequest | ImprovePreparationStep,
    *,
    github_port: ImprovePreparationGithubPort,
    short_sid: str | None = None,
    candidate_budget: int | None = None,
) -> PreparedImproveStep:
    """Prepare the exact `RunRequest` payload for one Improve step.

    Callers can either pass an explicit `ImproveStepPreparationRequest` or a
    driver-produced step plus `short_sid`. GitHub reads needed for scope args
    are performed through `github_port`, and any read error is allowed to
    propagate to the caller unchanged.
    """

    request = _coerce_request(
        request_or_step,
        short_sid=short_sid,
        candidate_budget=candidate_budget,
    )
    scope_args = _build_scope_args(request, github_port=github_port)
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


def _compute_work_body(
    step: ImprovePreparationStep,
    *,
    candidate_budget: int | None,
) -> str:
    template = step.cfg.template
    if template is PromptTemplate.IMPROVE_SCAN:
        budget = candidate_budget or 0
        if budget == 1:
            return "picking 1 improvement"
        return f"picking up to {budget} improvements"
    candidate = step.candidate
    if candidate is not None:
        ordinal = step.candidate_ordinal
        total = step.scan_set_size
        if ordinal is not None and total is not None:
            if template is PromptTemplate.IMPROVE_PRD:
                return (
                    f'writing spec for candidate {ordinal}/{total} "{candidate.title}"'
                )
            if template is PromptTemplate.IMPROVE_ISSUES:
                return f'filing tickets for candidate {ordinal}/{total} "{candidate.title}"'
    return step.cfg.display_body


def _coerce_request(
    request_or_step: ImproveStepPreparationRequest | ImprovePreparationStep,
    *,
    short_sid: str | None,
    candidate_budget: int | None,
) -> ImproveStepPreparationRequest:
    if isinstance(request_or_step, ImproveStepPreparationRequest):
        return request_or_step
    if short_sid is None:
        raise TypeError("short_sid is required when preparing from a driver step")

    step = cast("ImprovePreparationStep", request_or_step)
    return ImproveStepPreparationRequest(
        prompt_template=step.cfg.template,
        session_namespace=step.cfg.namespace,
        display_name=step.cfg.display_name,
        work_body=_compute_work_body(step, candidate_budget=candidate_budget),
        kind=step.kind,
        short_sid=short_sid,
        fetch_recent_prd_titles=step.fetch_recent_prd_titles,
        candidate_budget=candidate_budget,
        candidate=step.candidate,
    )


def _build_scope_args(
    request: ImproveStepPreparationRequest,
    *,
    github_port: ImprovePreparationGithubPort,
) -> dict[str, str]:
    if request.fetch_recent_prd_titles:
        return _build_improve_scope_args(request, github_port=github_port)
    if request.prompt_template.scope is Scope.IMPROVE_SCAN:
        return _build_scan_scope_args(request, recent_prds=[])
    if request.prompt_template.scope in (Scope.IMPROVE_ISSUES, Scope.IMPROVE_SESSION):
        return _build_improve_scope_args(request, github_port=github_port)
    return {}


def _build_scan_scope_args(
    request: ImproveStepPreparationRequest,
    *,
    recent_prds: list[dict[str, Any]],
) -> dict[str, str]:
    if request.candidate_budget is None:
        raise PromptRenderError(
            "candidate_budget is required to render the improve scan prompt"
        )
    return build_improve_scan_scope_args(
        recent_prds=recent_prds,
        candidate_budget=request.candidate_budget,
    )


def _build_improve_scope_args(
    request: ImproveStepPreparationRequest,
    *,
    github_port: ImprovePreparationGithubPort,
) -> dict[str, str]:
    template = request.prompt_template
    if template is PromptTemplate.IMPROVE_SCAN:
        return _build_scan_scope_args(
            request,
            recent_prds=github_port.get_recent_improve_prds(),
        )

    if template is PromptTemplate.IMPROVE_PRD:
        if request.candidate is None:
            raise PromptRenderError("candidate is required to render the spec prompt")
        return validated_scope_args_for_template(
            template,
            {
                "IMPROVE_SHORT_SID": request.short_sid,
                "RECENT_IMPROVE_PRDS": _format_recent_improve_prds(
                    github_port.get_recent_improve_prds()
                ),
                "CANDIDATE_RANK": str(request.candidate.rank),
                "CANDIDATE_TITLE": request.candidate.title,
            },
        )

    if template is PromptTemplate.IMPROVE_NO_CANDIDATE:
        return validated_scope_args_for_template(
            template,
            {
                "IMPROVE_SHORT_SID": request.short_sid,
                "RECENT_IMPROVE_PRDS": _format_recent_improve_prds(
                    github_port.get_recent_improve_prds()
                ),
                "CANDIDATE_RANK": "",
                "CANDIDATE_TITLE": "",
            },
        )

    if template is PromptTemplate.IMPROVE_ISSUES:
        return validated_scope_args_for_template(
            template,
            {"IMPROVE_SHORT_SID": request.short_sid},
        )

    raise TypeError(f"unsupported Improve template: {template.name}")


def _format_recent_improve_prds(recent_prds: list[dict[str, Any]]) -> str:
    if not recent_prds:
        return "No recent improve PRDs found."
    return "\n".join(
        f"#{prd['number']} {prd['state']} - {prd['title']}" for prd in recent_prds
    )
