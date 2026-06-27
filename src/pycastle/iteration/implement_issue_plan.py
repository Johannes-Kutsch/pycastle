from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from ..agents.output_protocol import AgentRole
from ..config import Config
from ..issue_readiness import require_ready_slice_outcome_for_issue
from ..managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import build_per_issue_scope_args
from ..services import GitService
from ..session import RoleSession, RunKind, is_stage_done_for
from ..session.service_session_store import (
    has_exact_provider_transcript_for_selected_service,
)
from ..infrastructure.worktree import issue_branch

if TYPE_CHECKING:
    from ..services import ServiceRegistry


RoleName: TypeAlias = Literal["implementer", "reviewer"]
IssueStage: TypeAlias = Literal["pre-implementation", "pre-review"]
StepOutcome: TypeAlias = Literal["skip", "run", "setup_failure"]
IssueOutcome: TypeAlias = Literal["complete", "incomplete"]


class ImplementIssuePlanDeps(Protocol):
    cfg: Config
    git_svc: GitService
    repo_root: Path
    service_registry: "ServiceRegistry | None"


@dataclasses.dataclass(frozen=True)
class CommitFallbackSubject:
    commit_prefix: str
    fallback_subject: str


@dataclasses.dataclass(frozen=True)
class MountSetupFailure:
    role_value: str
    rejection_code: str
    rejection: ManagedWorktreeMountRejected
    error_message: str


@dataclasses.dataclass(frozen=True)
class IssueRoleStepPlan:
    outcome: StepOutcome
    role_name: RoleName
    role: AgentRole
    stage: IssueStage
    run_kind: RunKind
    work_body: str
    prompt_template: PromptTemplate
    prompt_scope_args: dict[str, str]
    model: str
    effort: str
    service: str
    mount_setup_failure: MountSetupFailure | None
    commit_fallback_subject: CommitFallbackSubject | None
    skip_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class IssueExecutionPlan:
    issue_number: int
    issue_title: str
    branch: str
    planner_sha: str | None
    slice_mode_display_name: str
    issue_outcome: IssueOutcome
    implementer_step: IssueRoleStepPlan
    reviewer_step: IssueRoleStepPlan

    @property
    def steps(self) -> tuple[IssueRoleStepPlan, IssueRoleStepPlan]:
        return (self.implementer_step, self.reviewer_step)

    @property
    def run_steps(self) -> tuple[IssueRoleStepPlan, ...]:
        return tuple(step for step in self.steps if step.outcome == "run")


@dataclasses.dataclass(frozen=True)
class ReadyIssueSlicePlan:
    display_name: str
    implement_prompt_template: PromptTemplate
    implement_work_body: str
    review_work_body: str


def plan_ready_issue_slice(issue: dict, cfg: Config) -> ReadyIssueSlicePlan:
    ready = require_ready_slice_outcome_for_issue(issue, cfg)
    issue_title = issue["title"]
    return ReadyIssueSlicePlan(
        display_name=ready.display_name,
        implement_prompt_template=ready.template,
        implement_work_body=f'implementing {ready.display_name} "{issue_title}"',
        review_work_body=f'reviewing {ready.display_name} "{issue_title}"',
    )


def plan_issue_execution(
    *,
    issue: dict,
    deps: ImplementIssuePlanDeps,
    sha: str | None,
    implement_mount_path: Path,
    review_mount_path: Path,
    implement_done: bool,
    review_done: bool,
) -> IssueExecutionPlan:
    ready_slice = plan_ready_issue_slice(issue, deps.cfg)
    branch = issue_branch(issue["number"])
    return IssueExecutionPlan(
        issue_number=issue["number"],
        issue_title=issue["title"],
        branch=branch,
        planner_sha=sha,
        slice_mode_display_name=ready_slice.display_name,
        issue_outcome="complete" if review_done else "incomplete",
        implementer_step=_plan_step(
            issue=issue,
            deps=deps,
            branch=branch,
            role=AgentRole.IMPLEMENTER,
            stage="pre-implementation",
            prompt_template=ready_slice.implement_prompt_template,
            work_body=ready_slice.implement_work_body,
            mount_path=implement_mount_path,
            skip_reason=(
                "review stage already complete"
                if review_done
                else "implement stage already complete"
                if implement_done
                else None
            ),
        ),
        reviewer_step=_plan_step(
            issue=issue,
            deps=deps,
            branch=branch,
            role=AgentRole.REVIEWER,
            stage="pre-review",
            prompt_template=PromptTemplate.REVIEW,
            work_body=ready_slice.review_work_body,
            mount_path=review_mount_path,
            skip_reason="review stage already complete" if review_done else None,
        ),
    )


def plan_issue_execution_from_worktree(
    *,
    issue: dict,
    deps: ImplementIssuePlanDeps,
    sha: str | None,
    worktree_path: Path,
    implement_mount_path: Path,
    review_mount_path: Path,
) -> IssueExecutionPlan:
    return plan_issue_execution(
        issue=issue,
        deps=deps,
        sha=sha,
        implement_mount_path=implement_mount_path,
        review_mount_path=review_mount_path,
        implement_done=is_stage_done_for(worktree_path, AgentRole.IMPLEMENTER),
        review_done=is_stage_done_for(worktree_path, AgentRole.REVIEWER),
    )


def _plan_step(
    *,
    issue: dict,
    deps: ImplementIssuePlanDeps,
    branch: str,
    role: AgentRole,
    stage: IssueStage,
    prompt_template: PromptTemplate,
    work_body: str,
    mount_path: Path,
    skip_reason: str | None,
) -> IssueRoleStepPlan:
    role_name = _role_name(role)
    commit_fallback_subject = CommitFallbackSubject(
        commit_prefix=f"{'Implement' if role is AgentRole.IMPLEMENTER else 'Review'} #{issue['number']} - ",
        fallback_subject=(
            f"{'Implement' if role is AgentRole.IMPLEMENTER else 'Review'} "
            f"#{issue['number']} - {issue['title']}"
        ),
    )

    if skip_reason is not None:
        run_kind, interrupted_work_from_dirty_tree = _prompt_run_state_for_role(
            mount_path=mount_path,
            role=role,
            deps=deps,
        )
        prompt_scope_args = build_per_issue_scope_args(
            issue,
            branch=branch,
            run_kind=run_kind,
            is_dirty=interrupted_work_from_dirty_tree,
        )
        return IssueRoleStepPlan(
            outcome="skip",
            role_name=role_name,
            role=role,
            stage=stage,
            run_kind=run_kind,
            work_body=work_body,
            prompt_template=prompt_template,
            prompt_scope_args=prompt_scope_args,
            model=_resolved_stage_model(deps.cfg, role),
            effort=_resolved_stage_effort(deps.cfg, role),
            service=_resolved_stage_service_name(deps.cfg, role),
            mount_setup_failure=None,
            commit_fallback_subject=commit_fallback_subject,
            skip_reason=skip_reason,
        )

    mount_decision = decide_managed_worktree_mount(
        repo_root=deps.repo_root,
        mount_path=mount_path,
        caller=f"{'Implement' if role is AgentRole.IMPLEMENTER else 'Review'} Agent #{issue['number']}",
        role=role.value,
    )
    if isinstance(mount_decision, ManagedWorktreeMountRejected) and (
        should_reject_managed_worktree_mount(mount_decision)
    ):
        return IssueRoleStepPlan(
            outcome="setup_failure",
            role_name=role_name,
            role=role,
            stage=stage,
            run_kind=RunKind.FRESH,
            work_body=work_body,
            prompt_template=prompt_template,
            prompt_scope_args=build_per_issue_scope_args(
                issue,
                branch=branch,
                run_kind=RunKind.FRESH,
                is_dirty=False,
            ),
            model=_resolved_stage_model(deps.cfg, role),
            effort=_resolved_stage_effort(deps.cfg, role),
            service=_resolved_stage_service_name(deps.cfg, role),
            mount_setup_failure=MountSetupFailure(
                role_value=mount_decision.role or role.value,
                rejection_code=mount_decision.rejection_code,
                rejection=mount_decision,
                error_message=describe_managed_worktree_mount_rejection(mount_decision),
            ),
            commit_fallback_subject=commit_fallback_subject,
        )

    run_kind, interrupted_work_from_dirty_tree = _prompt_run_state_for_role(
        mount_path=mount_path,
        role=role,
        deps=deps,
    )
    prompt_scope_args = build_per_issue_scope_args(
        issue,
        branch=branch,
        run_kind=run_kind,
        is_dirty=interrupted_work_from_dirty_tree,
    )
    return IssueRoleStepPlan(
        outcome="run",
        role_name=role_name,
        role=role,
        stage=stage,
        run_kind=run_kind,
        work_body=work_body,
        prompt_template=prompt_template,
        prompt_scope_args=prompt_scope_args,
        model=_resolved_stage_model(deps.cfg, role),
        effort=_resolved_stage_effort(deps.cfg, role),
        service=_resolved_stage_service_name(deps.cfg, role),
        mount_setup_failure=None,
        commit_fallback_subject=commit_fallback_subject,
    )


def _role_name(role: AgentRole) -> RoleName:
    if role is AgentRole.IMPLEMENTER:
        return "implementer"
    if role is AgentRole.REVIEWER:
        return "reviewer"
    raise RuntimeError(f"Unsupported role {role!r} for implement issue planning")


def _resolved_stage_service_name(cfg: Config, role: AgentRole) -> str:
    if role is AgentRole.IMPLEMENTER:
        return cfg.implement_override.service
    if role is AgentRole.REVIEWER:
        return cfg.review_override.service
    raise RuntimeError(f"Unsupported role {role!r} for implement issue planning")


def _resolved_stage_model(cfg: Config, role: AgentRole) -> str:
    if role is AgentRole.IMPLEMENTER:
        return cfg.implement_override.model
    if role is AgentRole.REVIEWER:
        return cfg.review_override.model
    raise RuntimeError(f"Unsupported role {role!r} for implement issue planning")


def _resolved_stage_effort(cfg: Config, role: AgentRole) -> str:
    if role is AgentRole.IMPLEMENTER:
        return cfg.implement_override.effort
    if role is AgentRole.REVIEWER:
        return cfg.review_override.effort
    raise RuntimeError(f"Unsupported role {role!r} for implement issue planning")


def _prompt_run_state_for_role(
    *,
    mount_path: Path,
    role: AgentRole,
    deps: ImplementIssuePlanDeps,
) -> tuple[RunKind, bool]:
    role_session = RoleSession(mount_path, role)
    service_name = _resolved_stage_service_name(deps.cfg, role)
    has_resumable_state = role_session.is_resumable()
    has_exact_transcript_handoff = has_exact_provider_transcript_for_selected_service(
        worktree=mount_path,
        role=role,
        namespace="",
        registry=deps.service_registry,
        service_name=service_name,
    )
    run_kind = (
        role_session.run_kind()
        if has_exact_transcript_handoff or not has_resumable_state
        else RunKind.FRESH
    )
    interrupted_work_from_dirty_tree = (
        run_kind is RunKind.FRESH
        and has_resumable_state
        and not has_exact_transcript_handoff
        and not deps.git_svc.is_working_tree_clean(mount_path)
    )
    return run_kind, interrupted_work_from_dirty_tree
