from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.runner import RunRequest
from pycastle.bug_reporter import file_unrepairable_draft_set_issue
from pycastle.iteration.improve_draft_set_resolution import (
    Unrepairable,
    resolve_draft_set,
)
from pycastle.iteration.improve_drafts import DraftSetValidationError, read_draft_set
from pycastle.iteration.improve_filing import GithubFilingPort, file_draft_set
from pycastle.iteration.improve_role_session_store import ImproveRoleSessionStore
from pycastle.iteration.preflight import PreflightReady
from pycastle.prompts.dispatch import PromptKind, build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import validated_scope_args_for_template
from pycastle.session import RoleSession

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.agents.runner import AgentRunnerProtocol
    from pycastle.config import Config
    from pycastle.display.status_display import StatusDisplay
    from pycastle.iteration.preflight import PreflightCache
    from pycastle.services import GitService
    from pycastle.services.github_service import GithubService

_DRAFTS_SUBDIR = "_drafts"


class _LifecycleDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    github_svc: GithubService
    git_svc: GitService
    repo_root: Path
    preflight_cache: PreflightCache
    improve_dispatched_count: int


@dataclass(frozen=True)
class Advance:
    completed_count: int


@dataclass(frozen=True)
class Stop:
    reason: Literal["cap-reached", "safe-sha-changed", "drafts-abandoned"]
    completed_count: int


CandidateOutcome = Advance | Stop


def _cap_reached(deps: _LifecycleDeps, completed_count: int) -> bool:
    return (
        deps.cfg.improve_max is not None
        and deps.improve_dispatched_count + completed_count >= deps.cfg.improve_max
    )


async def _file_improve_drafts(
    *,
    deps: _LifecycleDeps,
    role_session_dir: Path,
    sandbox_path: Path,
    candidate_idx: int,
    candidate_namespace: str,
) -> bool:
    draft_dir = role_session_dir / _DRAFTS_SUBDIR
    store = ImproveRoleSessionStore(role_session_dir)
    prev_spec = store.prev_filed_spec(candidate_idx)

    candidate_list = store.read_candidate_list()
    scan_set_size = len(candidate_list.candidates) if candidate_list is not None else 0
    candidate_title = (
        candidate_list.candidates[candidate_idx].title
        if candidate_list is not None and candidate_idx < len(candidate_list.candidates)
        else ""
    )
    candidate_ordinal = candidate_idx + 1

    async def _correction_callback(
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
            f" {candidate_ordinal}/{scan_set_size}"
            f' "{candidate_title}"'
            f" (attempt {attempt + 1}/{total_attempts})"
        )
        await deps.agent_runner.run(
            RunRequest(
                name="Draft Correction",
                prompt=correction_prompt,
                mount_path=sandbox_path,
                role=AgentRole.IMPROVE,
                model=deps.cfg.improve_override.model,
                effort=deps.cfg.improve_override.effort,
                service=deps.cfg.improve_override.service,
                stage="improve-sandbox",
                status_display=deps.status_display,
                work_body=correction_body,
                session_namespace=candidate_namespace,
                preserve_session_on_completion=True,
            )
        )

    try:
        outcome = await resolve_draft_set(
            draft_dir=draft_dir,
            cfg=deps.cfg,
            correction_callback=_correction_callback,
        )
        if isinstance(outcome, Unrepairable):
            file_unrepairable_draft_set_issue(
                problems=outcome.problems,
                draft_files=outcome.draft_files,
                github_svc=deps.github_svc,
            )
            return False
        file_draft_set(
            outcome.drafts,
            port=GithubFilingPort(deps.github_svc),
            store=store,
            candidate_idx=candidate_idx,
            state_label=deps.cfg.issue_label,
            prev_spec=prev_spec,
        )
        return True
    finally:
        if draft_dir.is_dir():
            shutil.rmtree(draft_dir)


def _wind_down_partial_candidates(
    role_session_dir: Path,
    *,
    port: GithubFilingPort,
    cfg: Config,
) -> None:
    store = ImproveRoleSessionStore(role_session_dir)

    for pending in store.pending_candidates(0):
        record = pending.record
        if record is None or record.spec_number is None:
            continue
        if not record.filed_tickets:
            port.close_issue(record.spec_number)
        else:
            draft_dir = role_session_dir / _DRAFTS_SUBDIR
            try:
                drafts = read_draft_set(draft_dir, cfg)
                file_draft_set(
                    drafts,
                    port=port,
                    store=store,
                    candidate_idx=pending.index,
                    state_label=cfg.issue_label,
                    prev_spec=store.prev_filed_spec(pending.index),
                )
            except DraftSetValidationError:
                pass


def reconcile_and_wind_down(
    pre_sandbox_path: Path,
    *,
    fingerprint: str,
    port: GithubFilingPort,
    cfg: Config,
) -> None:
    """Pre-phase call: wind down partial candidates and discard the stale session if the fingerprint changed."""
    pre_role_session = RoleSession(pre_sandbox_path, AgentRole.IMPROVE)
    if pre_role_session.read_fingerprint() != fingerprint:
        _wind_down_partial_candidates(pre_role_session.path, port=port, cfg=cfg)
        pre_role_session.discard()


async def file_and_decide(
    *,
    step_namespace: str,
    deps: _LifecycleDeps,
    role_session_dir: Path,
    sandbox_path: Path,
    fingerprint: str,
    completed_count: int,
) -> CandidateOutcome:
    """Post-tickets call: file drafts and return a typed outcome.

    Returns Advance when the loop should continue to the next candidate.
    Returns Stop with reason cap-reached or safe-sha-changed when the loop
    should break.  Returns Stop with reason drafts-abandoned when the draft
    set was unrepairable; the outer loop continues to the next candidate
    without incrementing the completed count.
    """
    from pycastle.iteration.improve import _parse_candidate_namespace  # noqa: PLC0415

    candidate_idx = _parse_candidate_namespace(step_namespace)
    filed = await _file_improve_drafts(
        deps=deps,
        role_session_dir=role_session_dir,
        sandbox_path=sandbox_path,
        candidate_idx=candidate_idx,
        candidate_namespace=step_namespace,
    )
    if not filed:
        return Stop(reason="drafts-abandoned", completed_count=completed_count)
    new_count = completed_count + 1
    if _cap_reached(deps, new_count):
        return Stop(reason="cap-reached", completed_count=new_count)
    mid_verdict = await deps.preflight_cache.get_safe_sha(deps)
    sha_changed = (
        not isinstance(mid_verdict, PreflightReady) or mid_verdict.sha != fingerprint
    )
    if sha_changed:
        return Stop(reason="safe-sha-changed", completed_count=new_count)
    return Advance(completed_count=new_count)
