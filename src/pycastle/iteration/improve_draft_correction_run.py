from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.runner import RunRequest
from pycastle.prompts.dispatch import PromptKind, build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import validated_scope_args_for_template

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from pycastle.agents.runner import AgentRunnerProtocol
    from pycastle.config.types import StageOverride
    from pycastle.display.status_display import StatusDisplay
    from pycastle.iteration.improve_drafts import DraftSetValidationError


@dataclass(frozen=True)
class CorrectionRunContext:
    candidate_ordinal: int
    scan_set_size: int
    candidate_title: str
    candidate_namespace: str
    sandbox_path: Path
    agent_runner: AgentRunnerProtocol
    improve_override: StageOverride
    status_display: StatusDisplay


def make_correction_callback(
    ctx: CorrectionRunContext,
) -> Callable[[DraftSetValidationError, int, int], Awaitable[None]]:
    async def _callback(
        exc: DraftSetValidationError, attempt: int, total_attempts: int
    ) -> None:
        validation_errors = "\n".join(exc.problems)
        correction_prompt = build_prompt_invocation(
            PromptTemplate.IMPROVE_DRAFT_CORRECTION,
            validated_scope_args_for_template(
                PromptTemplate.IMPROVE_DRAFT_CORRECTION,
                {"VALIDATION_ERRORS": validation_errors},
            ),
            kind=PromptKind.FOLLOW_UP,
        )
        correction_body = (
            f"fixing draft validation errors for candidate"
            f" {ctx.candidate_ordinal}/{ctx.scan_set_size}"
            f' "{ctx.candidate_title}"'
            f" (attempt {attempt + 1}/{total_attempts})"
        )
        await ctx.agent_runner.run(
            RunRequest(
                name="Draft Correction",
                prompt=correction_prompt,
                mount_path=ctx.sandbox_path,
                role=AgentRole.IMPROVE,
                model=ctx.improve_override.model,
                effort=ctx.improve_override.effort,
                service=ctx.improve_override.service,
                stage="improve-sandbox",
                status_display=ctx.status_display,
                work_body=correction_body,
                session_namespace=ctx.candidate_namespace,
                preserve_session_on_completion=True,
            )
        )

    return _callback
