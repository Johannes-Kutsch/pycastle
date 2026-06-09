from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..prompts.pipeline import PromptTemplate, Scope
from ..prompts.scope_args import build_improve_scope_args


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


@dataclass(frozen=True)
class ImproveStepPreparationRequest:
    """Inputs required to prepare a single Improve step.

    `short_sid` is required for session-scoped placeholders. `prd_number` is
    required only when preparing `PromptTemplate.IMPROVE_ISSUES`; `None`
    preserves the current empty-placeholder fallback. `fetch_recent_prd_titles`
    preserves the existing scan-step retry behavior that skips the GitHub read.
    """

    prompt_template: PromptTemplate
    session_namespace: str
    display_name: str
    work_body: str
    send_role_prompt_on_resume: bool
    short_sid: str
    prd_number: int | None
    fetch_recent_prd_titles: bool


@dataclass(frozen=True)
class PreparedImproveStep:
    prompt_template: PromptTemplate
    session_namespace: str
    display_name: str
    work_body: str
    send_role_prompt_on_resume: bool
    scope_args: dict[str, str]


def prepare_improve_step(
    request: ImproveStepPreparationRequest,
    *,
    github_port: ImprovePreparationGithubPort,
) -> PreparedImproveStep:
    """Prepare the exact `RunRequest` payload for one Improve step.

    The caller must provide all display/session inputs through `request`.
    GitHub reads needed for scope args are performed through `github_port`, and
    any read error is allowed to propagate to the caller unchanged.
    """

    scope_args = _build_scope_args(request, github_port=github_port)
    return PreparedImproveStep(
        prompt_template=request.prompt_template,
        session_namespace=request.session_namespace,
        display_name=request.display_name,
        work_body=request.work_body,
        send_role_prompt_on_resume=request.send_role_prompt_on_resume,
        scope_args=scope_args,
    )


def _build_scope_args(
    request: ImproveStepPreparationRequest,
    *,
    github_port: ImprovePreparationGithubPort,
) -> dict[str, str]:
    if request.fetch_recent_prd_titles:
        return build_improve_scope_args(
            request.prompt_template,
            github_svc=github_port,
            short_sid=request.short_sid,
            prd_number=request.prd_number,
        )
    if request.prompt_template.scope is Scope.IMPROVE_SCAN:
        return build_improve_scope_args(
            request.prompt_template,
            github_svc=github_port,
            short_sid=request.short_sid,
            recent_prds=[],
        )
    if request.prompt_template.scope in (Scope.IMPROVE_ISSUES, Scope.IMPROVE_SESSION):
        return build_improve_scope_args(
            request.prompt_template,
            github_svc=github_port,
            short_sid=request.short_sid,
            prd_number=request.prd_number,
        )
    return {}
