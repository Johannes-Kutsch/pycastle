import dataclasses
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from ..agents.output_protocol import AgentRole, IssueOutput
from ..agents.result import CancellationToken
from ..agents.runner import RunRequest
from ..errors import AgentFailedError, AgentTimeoutError, UsageLimitError
from ..prompts.pipeline import PromptTemplate
from ..worktree import worktree_name_for_branch, worktree_path
from ._deps import Deps
from ._rows import PhaseRow as PhaseRow
from ._rows import agent_row as agent_row
from ._rows import phase_row
from .dispatcher import Done as Done
from .dispatcher import should_dispatch_improve
from .implement import branch_for, implement_phase
from .improve import ImproveContinue as ImproveContinue
from .improve import ImproveNoCandidate as ImproveNoCandidate
from .improve import improve_phase
from .merge import merge_phase
from .planning import AllBlocked as AllBlocked
from .planning import PlanReady as PlanReady
from .planning import planning_phase
from .preflight import (
    PreflightAFK,
    PreflightCache as PreflightCache,
    PreflightHITL,
    strip_stale_blocker_refs,
)


@dataclasses.dataclass(frozen=True)
class Continue:
    pass


@dataclasses.dataclass(frozen=True)
class AbortedHITL:
    issue_number: int


@dataclasses.dataclass(frozen=True)
class AbortedUsageLimit:
    reset_time: datetime | None = None


@dataclasses.dataclass(frozen=True)
class NoCandidate:
    pass


@dataclasses.dataclass(frozen=True)
class AbortedAgentFailure:
    failed_role: str
    issue_number: int | None = None


@dataclasses.dataclass(frozen=True)
class AbortedTimeout:
    failed_role: str
    worktree_path: Path


IterationOutcome: TypeAlias = (
    Continue
    | Done
    | AbortedHITL
    | AbortedUsageLimit
    | NoCandidate
    | AbortedAgentFailure
    | AbortedTimeout
)


def _is_in_flight(issue: dict, deps: Deps) -> bool:
    branch = branch_for(issue["number"])
    if deps.git_svc.verify_ref_exists(branch, deps.repo_root):
        return True
    name = worktree_name_for_branch(branch)
    return worktree_path(name, deps).exists()


async def _run_implement_and_merge(
    issues: list[dict],
    deps: Deps,
    sha: str,
) -> IterationOutcome:
    token = CancellationToken()
    async with phase_row(
        deps.status_display, "Implement", initial_phase="Running"
    ) as row:
        impl_result = await implement_phase(issues, deps, sha, token=token)

        if impl_result.usage_limit_hit:
            row.close("finished")
            return AbortedUsageLimit(reset_time=impl_result.usage_limit_reset_time)

        for issue, error in impl_result.errors:
            deps.status_display.print(
                "Implement",
                f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) failed: {error}",
            )

        completed = impl_result.completed

        if not completed:
            row.close(
                "No commits produced. Nothing to merge.", shutdown_style="warning"
            )
            return Continue()

        branch_lines = [f"  {branch_for(i['number'])}" for i in completed]
        row.close(
            "\n".join(
                [f"Execution complete, {len(completed)} branch(es) with commits:"]
                + branch_lines
            )
        )

    await merge_phase(completed, deps)
    return Continue()


async def _handle_preflight_outcome(
    result: PreflightHITL | PreflightAFK, deps: Deps
) -> IterationOutcome:
    if isinstance(result, PreflightHITL):
        deps.status_display.print(
            "Preflight",
            f"Preflight issue #{result.issue_number} requires human intervention. Exiting.",
        )
        return AbortedHITL(issue_number=result.issue_number)
    raw = deps.github_svc.get_issue(result.issue_number)
    afk_issue: dict = {**raw, "body": raw.get("body") or "", "comments": []}
    return await _run_implement_and_merge([afk_issue], deps, result.sha)


async def run_iteration(deps: Deps) -> IterationOutcome:
    try:
        # ── Fetch issues ─────────────────────────────────────────────────────
        open_issues = strip_stale_blocker_refs(
            deps.github_svc.get_open_issues(deps.cfg.issue_label)
        )
        all_open_issues = deps.github_svc.get_all_open_issues_lightweight()

        in_flight = [i for i in open_issues if _is_in_flight(i, deps)]

        # ── (Improve) — runs when idle: no open issues, no in-flight ────────
        if not open_issues and not in_flight:
            if should_dispatch_improve(
                deps.improve_mode,
                deps.slept_once,
                deps.improve_dispatched_count,
                deps.cfg.improve_max,
            ):
                improve_result = await improve_phase(deps)
                deps.improve_dispatched_count += 1
                if isinstance(improve_result, ImproveNoCandidate):
                    return NoCandidate()
                if isinstance(improve_result, (PreflightHITL, PreflightAFK)):
                    return await _handle_preflight_outcome(improve_result, deps)
                # ImproveContinue: re-fetch issues after improve filed new ones
                open_issues = strip_stale_blocker_refs(
                    deps.github_svc.get_open_issues(deps.cfg.issue_label)
                )
                all_open_issues = deps.github_svc.get_all_open_issues_lightweight()
                if not open_issues:
                    return Continue()
                in_flight = [i for i in open_issues if _is_in_flight(i, deps)]
            else:
                cap_hit = (
                    deps.cfg.improve_max is not None
                    and deps.improve_dispatched_count >= deps.cfg.improve_max
                    and deps.improve_mode is not None
                )
                return Done(improve_cap_reached=cap_hit)

        # ── Plan ─────────────────────────────────────────────────────────────
        plan_result = await planning_phase(
            deps, open_issues, all_open_issues, in_flight=in_flight
        )
        if isinstance(plan_result, AllBlocked):
            return Done()
        if isinstance(plan_result, (PreflightHITL, PreflightAFK)):
            return await _handle_preflight_outcome(plan_result, deps)

        issues: list[dict] = plan_result.issues

        # ── Implement ────────────────────────────────────────────────────────
        issues = issues[: deps.cfg.max_parallel]
        return await _run_implement_and_merge(issues, deps, plan_result.sha)

    except AgentFailedError as err:
        issue_number: int | None = None
        if deps.cfg.diagnose_on_failure:
            result = await deps.agent_runner.run(
                RunRequest(
                    name="Failure Report Agent",
                    template=PromptTemplate.FAILURE_REPORT,
                    mount_path=err.worktree_path,
                    role=AgentRole.FAILURE_REPORT,
                    scope_args={
                        "FAILED_ROLE": err.role_value,
                        "SESSION_DIR": err.session_dir,
                        "FAILURE_CLASS": err.failure_class,
                    },
                    status_display=deps.status_display,
                )
            )
            if isinstance(result, IssueOutput):
                issue_number = result.number
        return AbortedAgentFailure(
            failed_role=err.role_value, issue_number=issue_number
        )
    except UsageLimitError as err:
        return AbortedUsageLimit(reset_time=err.reset_time)
    except AgentTimeoutError as err:
        return AbortedTimeout(
            failed_role=err.role_value,
            worktree_path=err.worktree_path or deps.repo_root,
        )
