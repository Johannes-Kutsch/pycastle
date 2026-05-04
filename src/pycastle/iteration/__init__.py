import dataclasses
from datetime import datetime
from typing import TypeAlias

from ..agent_result import CancellationToken, PreflightFailure
from ..errors import UsageLimitError
from ..worktree import worktree_name_for_branch, worktree_path
from ._deps import Deps
from ._rows import PhaseRow as PhaseRow
from ._rows import agent_row as agent_row
from ._rows import phase_row
from .implement import branch_for, implement_phase
from .merge import merge_phase
from .planning import PlanReady as PlanReady
from .planning import planning_phase
from .preflight import PreflightAFK, PreflightHITL, PreflightReady, preflight_phase


@dataclasses.dataclass(frozen=True)
class Continue:
    pass


@dataclasses.dataclass(frozen=True)
class Done:
    pass


@dataclasses.dataclass(frozen=True)
class AbortedHITL:
    issue_number: int


@dataclasses.dataclass(frozen=True)
class AbortedUsageLimit:
    reset_time: datetime | None = None


IterationOutcome: TypeAlias = Continue | Done | AbortedHITL | AbortedUsageLimit


def _is_in_flight(issue: dict, deps: Deps) -> bool:
    branch = branch_for(issue["number"])
    if deps.git_svc.verify_ref_exists(branch, deps.repo_root):
        return True
    name = worktree_name_for_branch(branch)
    return worktree_path(name, deps).exists()


async def run_iteration(deps: Deps) -> IterationOutcome:
    async with phase_row(
        deps.status_display,
        "Preflight",
        initial_phase="Running",
    ) as preflight_row:
        try:
            preflight_result = await preflight_phase(deps)
        except UsageLimitError as err:
            preflight_row.close("finished")
            return AbortedUsageLimit(reset_time=err.reset_time)
        preflight_row.close("finished")

    if isinstance(preflight_result, PreflightHITL):
        deps.status_display.print(
            "Preflight",
            f"Preflight issue #{preflight_result.issue_number} requires human intervention. Exiting.",
        )
        return AbortedHITL(issue_number=preflight_result.issue_number)

    if isinstance(preflight_result, PreflightReady):
        if not preflight_result.issues:
            return Done()
        sha = preflight_result.sha
        open_issues = preflight_result.issues
        in_flight = [i for i in open_issues if _is_in_flight(i, deps)]
        if in_flight:
            issues = in_flight
        elif len(open_issues) >= 2:
            async with phase_row(
                deps.status_display,
                "Plan",
                initial_phase="Planning",
                startup_message=f"started planning for {len(open_issues)} issue(s) labeled {deps.cfg.issue_label}",
            ) as row:
                plan_result = await planning_phase(deps, sha, open_issues)
                issue_lines = [
                    f"  #{i['number']}: {i['title']} → {branch_for(i['number'])}"
                    for i in plan_result.issues
                ]
                row.close(
                    "\n".join(
                        [f"Planning complete, implementing {len(plan_result.issues)} issue(s):"] + issue_lines
                    )
                )
                sha = plan_result.worktree_sha
                issues = plan_result.issues
        else:
            issues = open_issues
    elif isinstance(preflight_result, PreflightAFK):
        sha = preflight_result.worktree_sha
        issues = preflight_result.issues

    issues = issues[: deps.cfg.max_parallel]

    token = CancellationToken()
    async with phase_row(deps.status_display, "Implement", initial_phase="Running") as row:
        impl_result = await implement_phase(issues, sha, deps, token=token)

        if impl_result.usage_limit_hit:
            row.close("finished")
            return AbortedUsageLimit(reset_time=impl_result.usage_limit_reset_time)

        for issue, error in impl_result.errors:
            match error:
                case PreflightFailure(failures=fs):
                    deps.status_display.print(
                        "Implement",
                        f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) pre-flight failed:",
                    )
                    for check_name, command, output in fs:
                        deps.status_display.print(
                            "Implement",
                            f"    ✗ {check_name} ({command}): {output}",
                        )
                case _:
                    deps.status_display.print(
                        "Implement",
                        f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) failed: {error}",
                    )

        completed = impl_result.completed

        if not completed:
            row.close("No commits produced. Nothing to merge.", shutdown_style="warning")
            return Continue()

        branch_lines = [f"  {branch_for(i['number'])}" for i in completed]
        row.close(
            "\n".join(
                [f"Execution complete, {len(completed)} branch(es) with commits:"] + branch_lines
            )
        )

    await merge_phase(completed, deps)

    return Continue()
