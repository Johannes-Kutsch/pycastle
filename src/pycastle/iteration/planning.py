import dataclasses
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    PlannerOutput,
)
from pycastle.agents.runner import AgentRunnerProtocol, RunRequest
from pycastle.agents.slice_classifier import (
    SliceClassifierVerdict,
    classify_slice,
)
from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.errors import SetupPhaseError
from pycastle.execution_contracts import WorktreeMount
from pycastle.infrastructure.worktree import (
    SandboxWorktreeIntent,
    reusable_sandbox_worktree,
    reusable_sandbox_worktree_identity,
)
from pycastle.iteration import planning_issue_intake
from pycastle.iteration._fingerprint import prepare_fingerprint_gate
from pycastle.iteration._rows import StatusRow, StatusRowConfig, status_row
from pycastle.iteration.implement import branch_for
from pycastle.iteration.planning_issue_intake import (
    PlanReady,
    PreparedPlanningIssueSet,
    apply_slice_classifier_verdicts,
)
from pycastle.iteration.preflight import PreflightAFK, PreflightCache, PreflightHITL
from pycastle.iteration.startable import startable_issues
from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from pycastle.prompts.dispatch import build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import build_plan_scope_args
from pycastle.services import GitService
from pycastle.services.github_service import GithubService
from pycastle.services.service_registry import ServiceRegistry
from pycastle.session import RoleSession

if TYPE_CHECKING:
    from pycastle.execution_contracts import PromptRuntimeExecutionAdapter

type _ClassifySliceFn = Callable[[str, str], Awaitable[SliceClassifierVerdict]]


class _PlanningDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    preflight_cache: PreflightCache
    service_registry: ServiceRegistry | None


@dataclasses.dataclass(frozen=True)
class AllBlocked:
    blocked: list[dict]


async def _run_slice_classifiers(
    deps: _PlanningDeps,
    wt: Path,
    issue_set: PreparedPlanningIssueSet,
    classify_fn: _ClassifySliceFn | None,
) -> dict[int, SliceClassifierVerdict]:
    service_registry = deps.service_registry
    if classify_fn is None and service_registry is None:
        return {}

    malformed_body_numbers = {i["number"] for i in issue_set.malformed_body_issues}
    verdicts: dict[int, SliceClassifierVerdict] = {}

    for issue in issue_set.malformed_slice_mode_issues:
        number = issue["number"]
        if number in malformed_body_numbers:
            continue

        title = issue.get("title") or ""
        body = issue.get("body") or ""

        if classify_fn is not None:
            verdict = await classify_fn(title, body)
        else:
            verdict = await classify_slice(
                issue_title=title,
                issue_body=body,
                worktree=WorktreeMount(host_path=wt),
                plan_override=deps.cfg.plan_override,
                runner=cast("PromptRuntimeExecutionAdapter", deps.agent_runner),
                service_registry=service_registry,  # type: ignore[arg-type]
            )
        verdicts[number] = verdict

    return verdicts


async def _relabel_issue_set(
    deps: _PlanningDeps,
    wt: Path,
    issue_set: PreparedPlanningIssueSet,
    classify_fn: _ClassifySliceFn | None,
) -> tuple[PreparedPlanningIssueSet, list[dict]]:
    if _classification_work_exists(issue_set):
        verdicts = await _run_slice_classifiers(deps, wt, issue_set, classify_fn)
        if verdicts:
            issue_set = apply_slice_classifier_verdicts(issue_set, verdicts, deps.cfg)
    return issue_set, list(issue_set.ready_candidates)


def _sync_labels(deps: _PlanningDeps, issue_set: PreparedPlanningIssueSet) -> None:
    for action in issue_set.label_sync_actions:
        if action.intent == "add":
            deps.github_svc.add_label_to_issue(action.issue_number, action.label_name)
            if action.comment_body is not None:
                deps.github_svc.post_comment(action.issue_number, action.comment_body)
            continue
        deps.github_svc.remove_label_from_issue(action.issue_number, action.label_name)


def _classification_work_exists(issue_set: PreparedPlanningIssueSet) -> bool:
    malformed_body_numbers = {i["number"] for i in issue_set.malformed_body_issues}
    return any(
        i["number"] not in malformed_body_numbers
        for i in issue_set.malformed_slice_mode_issues
    )


def _resolve_no_planner_path(
    row: StatusRow,
    deps: _PlanningDeps,
    issue_set: PreparedPlanningIssueSet,
    well_formed: list[dict],
    sha: str,
) -> PlanReady | AllBlocked:
    if not well_formed:
        blocker_summary = planning_issue_intake.planning_blocker_summary(
            issue_set.blocker_summary_inputs
        )
        lines = ["All ready-for-agent issues are blocked."]
        if blocker_summary:
            lines.append(blocker_summary)
        row.close("\n".join(lines))
        return AllBlocked(blocked=[])
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
            readiness_by_number=dict(issue_set.ready_readiness_by_number),
        ),
        issue_set,
    )


async def _run_planner_agent(
    deps: _PlanningDeps,
    wt: Path,
    well_formed: list[dict],
    all_open_issues: list[dict],
) -> PlannerOutput:
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
    return output


async def planning_phase(
    deps: _PlanningDeps,
    open_issues: list[dict],
    all_open_issues: list[dict],
    prepared_issue_set: PreparedPlanningIssueSet | None = None,
    in_flight: list[dict] | None = None,
    *,
    _classify_fn: _ClassifySliceFn | None = None,
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
        config=StatusRowConfig(
            initial_phase="Planning",
            startup_message=startup_msg,
        ),
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

        well_formed = startable_issues(
            list(issue_set.ready_candidates), in_flight=set()
        )
        _use_worktree = _classification_work_exists(issue_set) or len(well_formed) > 1

        if not _use_worktree:
            _sync_labels(deps, issue_set)
            return _resolve_no_planner_path(row, deps, issue_set, well_formed, sha)

        _sorted_ids = sorted(i["number"] for i in all_open_issues)
        fingerprint = hashlib.sha256(f"{sha}:{_sorted_ids}".encode()).hexdigest()
        _plan_sandbox_identity = reusable_sandbox_worktree_identity(
            SandboxWorktreeIntent.PLAN, deps.repo_root
        )
        _plan_sandbox_session = RoleSession(
            _plan_sandbox_identity.path, AgentRole.PLANNER
        )
        prepare_fingerprint_gate(_plan_sandbox_session, fingerprint)

        async with reusable_sandbox_worktree(
            SandboxWorktreeIntent.PLAN,
            sha=sha,
            deps=deps,
        ) as wt:
            _plan_sandbox_session.write_fingerprint(fingerprint)
            issue_set, relabeled = await _relabel_issue_set(
                deps, wt, issue_set, _classify_fn
            )
            well_formed = startable_issues(relabeled, in_flight=set())
            _sync_labels(deps, issue_set)

            if len(well_formed) <= 1:
                return _resolve_no_planner_path(row, deps, issue_set, well_formed, sha)

            output = await _run_planner_agent(deps, wt, well_formed, all_open_issues)

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
                        f"Planning complete, implementing {len(resolved.issues)} issue(s):",
                        *issue_lines,
                    ]
                )
            )
            return resolved


def _format_blocked_issue_line(blocked_issue: dict) -> str:
    number = blocked_issue["number"]
    if "title" in blocked_issue:
        return f"  #{number}: {blocked_issue['title']}"
    return f"  #{number}"


def _close_all_blocked_row(
    row: StatusRow, issue_set: PreparedPlanningIssueSet, blocked: list[dict]
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
