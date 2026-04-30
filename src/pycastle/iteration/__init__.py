import dataclasses
from typing import TypeAlias

from ..agent_result import CancellationToken, PreflightFailure
from ._deps import Deps
from .implement import branch_for, implement_phase
from .merge import merge_phase
from .plan import PlanHITL, PlanReady, PlanUsageLimit, plan_phase


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


async def run_iteration(deps: Deps) -> IterationOutcome:
    plan_result = await plan_phase(deps)

    if isinstance(plan_result, PlanUsageLimit):
        print("Usage limit reached during planning. Exiting.")
        return AbortedUsageLimit()

    if isinstance(plan_result, PlanHITL):
        print(
            f"Preflight issue #{plan_result.issue_number} requires human intervention. Exiting."
        )
        return AbortedHITL(issue_number=plan_result.issue_number)

    if isinstance(plan_result, PlanReady) and not plan_result.issues:
        return Done()

    sha = plan_result.worktree_sha
    issues = plan_result.issues[: deps.cfg.max_parallel]

    print(f"Planning complete. {len(issues)} issue(s):")
    for issue in issues:
        print(f"  #{issue['number']}: {issue['title']} → {branch_for(issue['number'])}")

    token = CancellationToken()
    impl_result = await implement_phase(issues, sha, deps, token=token)

    if impl_result.usage_limit_hit:
        return AbortedUsageLimit()

    for issue, error in impl_result.errors:
        match error:
            case PreflightFailure(failures=fs):
                print(
                    f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) pre-flight failed:"
                )
                for check_name, command, output in fs:
                    print(f"    ✗ {check_name} ({command}): {output}")
            case _:
                print(
                    f"  ✗ #{issue['number']} ({branch_for(issue['number'])}) failed: {error}"
                )

    completed = impl_result.completed

    if not completed:
        print("No commits produced. Nothing to merge.")
        return Continue()

    print(f"\nExecution complete. {len(completed)} branch(es) with commits:")
    for i in completed:
        print(f"  {branch_for(i['number'])}")

    await merge_phase(completed, deps)

    return Continue()
