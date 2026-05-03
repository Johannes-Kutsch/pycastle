import dataclasses
from typing import TypeAlias

from ..agent_result import CancellationToken, PreflightFailure
from ..worktree import worktree_name_for_branch, worktree_path
from ._deps import Deps
from .implement import branch_for, implement_phase
from .merge import merge_phase
from .preflight import PreflightAFK, PreflightHITL, PreflightReady, preflight_phase
from .planning import PlanReady, planning_phase


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
    pass


IterationOutcome: TypeAlias = Continue | Done | AbortedHITL | AbortedUsageLimit


def _is_in_flight(issue: dict, deps: Deps) -> bool:
    branch = branch_for(issue["number"])
    if deps.git_svc.verify_ref_exists(branch, deps.repo_root):
        return True
    name = worktree_name_for_branch(branch)
    return worktree_path(name, deps).exists()


async def run_iteration(deps: Deps) -> IterationOutcome:
    deps.status_display.register("Preflight")
    try:
        preflight_result = await preflight_phase(deps)
    finally:
        deps.status_display.remove("Preflight")

    if isinstance(preflight_result, PreflightHITL):
        deps.status_display.print(
            "",
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
            deps.status_display.register("Plan")
            try:
                plan_result = await planning_phase(deps, sha, open_issues)
            finally:
                deps.status_display.remove("Plan")
            sha = plan_result.worktree_sha
            issues = plan_result.issues
        else:
            issues = open_issues
    elif isinstance(preflight_result, PreflightAFK):
        sha = preflight_result.worktree_sha
        issues = preflight_result.issues

    issues = issues[: deps.cfg.max_parallel]

    deps.status_display.print("", f"Planning complete. {len(issues)} issue(s):")
    for issue in issues:
        deps.status_display.print(
            "",
            f"  #{issue['number']}: {issue['title']} → {branch_for(issue['number'])}",
        )

    token = CancellationToken()
    deps.status_display.register("Implement")
    try:
        impl_result = await implement_phase(issues, sha, deps, token=token)
    finally:
        deps.status_display.remove("Implement")

    if impl_result.usage_limit_hit:
        return AbortedUsageLimit()

    for issue, error in impl_result.errors:
        match error:
            case PreflightFailure(failures=fs):
                deps.status_display.print(
                    "",
                    f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) pre-flight failed:",
                )
                for check_name, command, output in fs:
                    deps.status_display.print(
                        "",
                        f"    ✗ {check_name} ({command}): {output}",
                    )
            case _:
                deps.status_display.print(
                    "",
                    f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) failed: {error}",
                )

    completed = impl_result.completed

    if not completed:
        deps.status_display.print("", "No commits produced. Nothing to merge.")
        return Continue()

    deps.status_display.print(
        "",
        f"Execution complete. {len(completed)} branch(es) with commits:",
    )
    for i in completed:
        deps.status_display.print("", f"  {branch_for(i['number'])}")

    await merge_phase(completed, deps)

    return Continue()
