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
from .dispatcher import DispatchImprove as DispatchImprove
from .dispatcher import Done as Done
from .dispatcher import RunImplementDirect as RunImplementDirect
from .dispatcher import RunPlan as RunPlan
from .dispatcher import decide_iteration_action
from .implement import branch_for, implement_phase
from .improve import improve_phase
from .merge import merge_phase
from .planning import PlanReady as PlanReady
from .planning import planning_phase
from .preflight import PreflightHITL, PreflightReady, preflight_phase


@dataclasses.dataclass(frozen=True)
class Continue:
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
    try:
        async with phase_row(
            deps.status_display,
            "Preflight",
            initial_phase="Running",
        ) as preflight_row:
            preflight_result = await preflight_phase(deps)
            preflight_row.close("finished")

        if isinstance(preflight_result, PreflightHITL):
            deps.status_display.print(
                "Preflight",
                f"Preflight issue #{preflight_result.issue_number} requires human intervention. Exiting.",
            )
            return AbortedHITL(issue_number=preflight_result.issue_number)

        preflight_sha = (
            preflight_result.sha
            if isinstance(preflight_result, PreflightReady)
            else preflight_result.worktree_sha
        )
        open_issues = preflight_result.issues
        in_flight = [i for i in open_issues if _is_in_flight(i, deps)]
        action = decide_iteration_action(
            open_afk_count=len(open_issues),
            in_flight_count=len(in_flight),
            improve_mode=deps.improve_mode,
            slept_once=deps.slept_once,
            improve_dispatched_this_iteration=deps.improve_dispatched_this_iteration,
        )
        match action:
            case Done():
                return Done()
            case DispatchImprove():
                await improve_phase(deps, sha=preflight_sha)
                return Continue()
            case RunImplementDirect():
                sha = preflight_sha
                issues = in_flight
            case RunPlan():
                sha = preflight_sha
                async with phase_row(
                    deps.status_display,
                    "Plan",
                    initial_phase="Planning",
                    startup_message=f"started planning for {len(open_issues)} issue(s) labeled {deps.cfg.issue_label}",
                ) as row:
                    all_open_issues = (
                        preflight_result.all_open_issues
                        if isinstance(preflight_result, PreflightReady)
                        else open_issues
                    )
                    plan_result = await planning_phase(
                        deps, sha, open_issues, all_open_issues
                    )
                    issue_lines = [
                        f"  #{i['number']}: {i['title']} → {branch_for(i['number'])}"
                        for i in plan_result.issues
                    ]
                    row.close(
                        "\n".join(
                            [
                                f"Planning complete, implementing {len(plan_result.issues)} issue(s):"
                            ]
                            + issue_lines
                        )
                    )
                    sha = plan_result.worktree_sha
                    issues = plan_result.issues

        issues = issues[: deps.cfg.max_parallel]

        token = CancellationToken()
        async with phase_row(
            deps.status_display, "Implement", initial_phase="Running"
        ) as row:
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
    except UsageLimitError as err:
        return AbortedUsageLimit(reset_time=err.reset_time)
