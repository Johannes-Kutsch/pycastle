import dataclasses
from pathlib import Path
from typing import Protocol

from ..agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    PlannerOutput,
)
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import build_plan_scope_args
from ..services import GitService
from ..services.github_service import GithubService
from ..display.status_display import StatusDisplay
from ..infrastructure.worktree import (
    DetachedTransientWorktreeIntent,
    detached_transient_worktree,
)
from ..managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from ..errors import SetupPhaseError
from ._rows import status_row
from .implement import branch_for
from .planning_issue_intake import PlanReady, PreparedPlanningIssueSet
from . import planning_issue_intake
from .preflight import PreflightAFK, PreflightCache, PreflightHITL


class _PlanningDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    preflight_cache: PreflightCache


@dataclasses.dataclass(frozen=True)
class AllBlocked:
    blocked: list[dict]


async def planning_phase(
    deps: _PlanningDeps,
    open_issues: list[dict],
    all_open_issues: list[dict],
    prepared_issue_set: PreparedPlanningIssueSet | None = None,
    in_flight: list[dict] | None = None,
) -> PlanReady | AllBlocked | PreflightHITL | PreflightAFK:
    _in_flight = in_flight or []
    issue_set = (
        prepared_issue_set
        if prepared_issue_set is not None
        else planning_issue_intake.prepare_planning_issue_set(open_issues, deps.cfg)
    )

    if _in_flight:
        startup_msg = f"checking {len(_in_flight)} in-flight branch(es) labeled {deps.cfg.issue_label}"
    else:
        startup_msg = f"started planning for {len(open_issues)} issue(s) labeled {deps.cfg.issue_label}"

    async with status_row(
        deps.status_display,
        "Plan",
        kind="phase",
        must_close=True,
        initial_phase="Planning",
        startup_message=startup_msg,
    ) as row:
        if _in_flight:
            verdict = await deps.preflight_cache.get_safe_sha(deps)
            if isinstance(verdict, (PreflightHITL, PreflightAFK)):
                row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
                return verdict
            nums = ", ".join(f"#{i['number']}" for i in _in_flight)
            row.close(
                f"resuming {len(_in_flight)} in-flight branch(es) ({nums}) labeled"
                f" {deps.cfg.issue_label}, skipping plan agent"
            )
            return PlanReady(issues=_in_flight, sha=verdict.sha)

        verdict = await deps.preflight_cache.get_safe_sha(deps)
        if isinstance(verdict, (PreflightHITL, PreflightAFK)):
            row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
            return verdict
        sha = verdict.sha

        for action in issue_set.label_sync_actions:
            if action.intent == "add":
                deps.github_svc.add_label_to_issue(
                    action.issue_number, action.label_name
                )
                if action.comment_body is not None:
                    deps.github_svc.post_comment(
                        action.issue_number, action.comment_body
                    )
                continue
            deps.github_svc.remove_label_from_issue(
                action.issue_number, action.label_name
            )

        well_formed = list(issue_set.ready_candidates)
        readiness_by_number = dict(issue_set.ready_readiness_by_number)

        if not well_formed:
            blocker_summary = planning_issue_intake.planning_blocker_summary(
                issue_set.blocker_summary_inputs
            )
            lines = ["All ready-for-agent issues are blocked."]
            if blocker_summary:
                lines.append(blocker_summary)
            row.close("\n".join(lines))
            return AllBlocked(blocked=[])

        if len(well_formed) == 1:
            row.close(
                f"only one open issue (#{well_formed[0]['number']}) labeled"
                f" {deps.cfg.issue_label}, skipping plan agent"
            )
            return planning_issue_intake.resolve_planner_issue_intake(
                PlanReady(
                    issues=[
                        {
                            "number": well_formed[0]["number"],
                            "title": well_formed[0]["title"],
                        }
                    ],
                    sha=sha,
                    readiness_by_number=readiness_by_number,
                ),
                issue_set,
            )

        async with detached_transient_worktree(
            DetachedTransientWorktreeIntent.PLAN,
            sha=sha,
            deps=deps,
        ) as wt:
            mount_decision = decide_managed_worktree_mount(
                repo_root=deps.repo_root,
                mount_path=wt,
                caller="Plan Agent",
                role=AgentRole.PLANNER.value,
            )
            if isinstance(
                mount_decision, ManagedWorktreeMountRejected
            ) and should_reject_managed_worktree_mount(mount_decision):
                raise SetupPhaseError(
                    AgentRole.PLANNER.value,
                    describe_managed_worktree_mount_rejection(mount_decision),
                )
            try:
                output = await deps.agent_runner.run(
                    RunRequest(
                        name="Plan Agent",
                        prompt=build_prompt_invocation(
                            PromptTemplate.PLAN,
                            build_plan_scope_args(
                                all_open_issues=all_open_issues,
                                ready_for_agent_issues=well_formed,
                            ),
                        ),
                        mount_path=wt,
                        role=AgentRole.PLANNER,
                        model=deps.cfg.plan_override.model,
                        effort=deps.cfg.plan_override.effort,
                        service=deps.cfg.plan_override.service,
                        stage="plan-sandbox",
                        status_display=deps.status_display,
                        work_body=f"Creating Plan from {len(well_formed)} issues",
                    )
                )
            except AgentOutputProtocolError as exc:
                raise RuntimeError(str(exc)) from exc

            if not isinstance(output, PlannerOutput):
                raise RuntimeError(
                    f"Planner returned unexpected output type: {type(output).__name__}"
                )
            if not output.issues:
                blocked = planning_issue_intake.resolve_planner_all_blocked_intake(
                    output, issue_set
                )
                _close_all_blocked_row(row, issue_set, blocked)
                return AllBlocked(blocked=blocked)

            resolved = planning_issue_intake.resolve_planner_issue_intake(
                PlanReady(issues=output.issues, sha=sha),
                issue_set,
            )
            if not resolved.issues:
                blocked = planning_issue_intake.resolve_planner_all_blocked_intake(
                    output, issue_set
                )
                _close_all_blocked_row(row, issue_set, blocked)
                return AllBlocked(blocked=blocked)
            issue_lines = [
                f"  #{i['number']}: {i['title']} → {branch_for(i['number'])}"
                for i in resolved.issues
            ]
            row.close(
                "\n".join(
                    [
                        f"Planning complete, implementing {len(resolved.issues)} issue(s):"
                    ]
                    + issue_lines
                )
            )
            return resolved


def _format_blocked_issue_line(blocked_issue: dict) -> str:
    number = blocked_issue["number"]
    if "title" in blocked_issue:
        return f"  #{number}: {blocked_issue['title']}"
    return f"  #{number}"


def _close_all_blocked_row(
    row, issue_set: PreparedPlanningIssueSet, blocked: list[dict]
) -> None:
    blocker_summary = planning_issue_intake.planning_blocker_summary(
        issue_set.blocker_summary_inputs
    )
    blocked_lines = [
        _format_blocked_issue_line(blocked_issue) for blocked_issue in blocked
    ]
    lines = [
        "All ready-for-agent issues are blocked:"
        if blocked_lines
        else "All ready-for-agent issues are blocked."
    ]
    if blocker_summary:
        lines.append(blocker_summary)
    lines.extend(blocked_lines)
    row.close("\n".join(lines))
