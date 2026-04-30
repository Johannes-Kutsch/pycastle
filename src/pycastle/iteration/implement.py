import asyncio
import contextlib
import dataclasses
from collections.abc import Sequence
from typing import Any

from ..agent_result import (
    AgentSuccess,
    CancellationToken,
    PreflightFailure,
    UsageLimitHit,
)
from ..prompt_utils import load_standards
from ._deps import Deps


def branch_for(issue_number: int) -> str:
    return f"pycastle/issue-{issue_number}"


def _format_feedback_commands(checks: Sequence[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


@dataclasses.dataclass
class ImplementResult:
    completed: list[dict]
    errors: list[tuple[dict, Exception | PreflightFailure]]
    usage_limit_hit: bool = False


async def run_issue(
    issue: dict,
    deps: Deps,
    semaphore: asyncio.Semaphore | None = None,
    *,
    token: CancellationToken | None = None,
    sha: str | None = None,
) -> dict | UsageLimitHit | PreflightFailure | None:
    _branch = branch_for(issue["number"])
    _standards = load_standards(deps.cfg.prompts_dir)
    prompt_args = {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "BRANCH": _branch,
        "FEEDBACK_COMMANDS": _format_feedback_commands(deps.cfg.implement_checks),
        **_standards,
    }

    async def _bounded_run_agent(**kwargs: Any) -> Any:
        async with semaphore or contextlib.nullcontext():
            return await deps.run_agent(**kwargs, token=token)

    result = await _bounded_run_agent(
        name=f"Implementer #{issue['number']}",
        prompt_file=deps.cfg.prompts_dir / "implement-prompt.md",
        mount_path=deps.repo_root,
        env=deps.env,
        prompt_args=prompt_args,
        branch=_branch,
        model=deps.cfg.implement_override.model,
        effort=deps.cfg.implement_override.effort,
        stage="pre-implementation",
        sha=sha,
        skip_preflight=True,
    )
    if isinstance(result, UsageLimitHit):
        return result
    if isinstance(result, PreflightFailure):
        return result
    if not isinstance(result, AgentSuccess):
        return None

    deps.logger.log_agent_output(f"Implementer #{issue['number']}", result.output)

    review_result = await _bounded_run_agent(
        name=f"Reviewer #{issue['number']}",
        prompt_file=deps.cfg.prompts_dir / "review-prompt.md",
        mount_path=deps.repo_root,
        env=deps.env,
        prompt_args=prompt_args,
        branch=_branch,
        model=deps.cfg.review_override.model,
        effort=deps.cfg.review_override.effort,
        stage="pre-review",
        skip_preflight=True,
    )
    if isinstance(review_result, UsageLimitHit):
        return review_result
    return issue


async def implement_phase(
    issues: list[dict],
    sha: str | None,
    deps: Deps,
    *,
    token: CancellationToken | None = None,
) -> ImplementResult:
    _token = token if token is not None else CancellationToken()
    semaphore = asyncio.Semaphore(deps.cfg.max_parallel)
    results = await asyncio.gather(
        *[run_issue(issue, deps, semaphore, token=_token, sha=sha) for issue in issues],
        return_exceptions=True,
    )
    usage_limit_hit = any(isinstance(r, UsageLimitHit) for r in results)
    completed: list[dict] = []
    errors: list[tuple[dict, Exception | PreflightFailure]] = []
    for issue, result in zip(issues, results):
        if isinstance(result, (Exception, PreflightFailure)):
            deps.logger.log_error(issue, result)
            errors.append((issue, result))
        elif isinstance(result, dict):
            completed.append(issue)
    return ImplementResult(
        completed=completed, errors=errors, usage_limit_hit=usage_limit_hit
    )
