from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from ..agent_credential_failure_routing import (
    route_agent_credential_failure,
)
from ..agents.output_protocol import AgentRole, IssueOutput
from ..agents.result import CancellationToken
from ..agents.runner import RunRequest
from ..bug_reporter import (
    BUG_REPORT_LABEL_LIST,
    auto_file_issue,
)
from ..errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    HardAgentError,
    ModelNotAvailableError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from ..diagnostic_mount_fallback import (
    DiagnosticMountFallbackIssue,
    decide_diagnostic_mount_dispatch,
)
from ..services import OperatorActionableGitError
from ..prompts.dispatch import build_prompt_invocation
from ..prompts.pipeline import PromptTemplate
from ..prompts.scope_args import build_failure_report_scope_args
from ._deps import Deps
from ._rows import StatusRow as StatusRow
from ._rows import status_row as status_row
from .implement import branch_for, implement_phase
from .in_flight import select_in_flight_issues
from .improve import ImproveContinue as ImproveContinue
from .improve import ImproveNoCandidate as ImproveNoCandidate
from .improve import improve_phase
from .merge import merge_phase
from .planning import AllBlocked as AllBlocked
from .planning import PlanReady as PlanReady
from .planning import planning_phase
from .planning_issue_intake import prepare_planning_issue_set
from .preflight import (
    PreflightAFK,
    PreflightCache as PreflightCache,
    PreflightHITL,
)

_FILED_USAGE_LIMIT_RAW_MESSAGES: set[str] = set()


_EVIDENCE_DIR = Path(".pycastle-session") / "failure-report"
_EVIDENCE_FILENAME = "agent-invocation.log"


def _evidence_relative_path() -> str:
    return (_EVIDENCE_DIR / _EVIDENCE_FILENAME).as_posix()


def _copy_invocation_log_to_evidence_area(
    *,
    worktree_path: Path,
    source: Path | str | None,
) -> Path | None:
    if source is None:
        return None
    if not worktree_path.is_dir():
        return None
    source_path = Path(source)
    if not source_path.is_file():
        return None

    destination = worktree_path / _EVIDENCE_DIR / _EVIDENCE_FILENAME
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        return destination
    except OSError:
        return None


def _extract_legacy_hard_error_text(raw: str) -> str:
    error_text = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("result"):
            error_text = str(parsed["result"])
        elif isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                data = error.get("data")
                if isinstance(data, dict) and data.get("message"):
                    error_text = str(data["message"])
                elif not isinstance(data, dict) and error.get("message"):
                    error_text = str(error["message"])
    except (json.JSONDecodeError, TypeError):
        pass
    return error_text


def _extract_hard_error_status_code(raw: str, fallback: int | None) -> int | None:
    if fallback is not None:
        return fallback
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    status = parsed.get("status")
    return (
        status if isinstance(status, int) and not isinstance(status, bool) else fallback
    )


def _route_and_abort_agent_credential_failure(
    err: HardAgentError,
    deps: Deps,
) -> "AbortedAgentCredentialFailure" | None:
    routed_failure = route_agent_credential_failure(
        provider_failure=err,
        github_svc=deps.github_svc,
    )
    if routed_failure is None:
        return None
    deps.status_display.print(
        err.caller,
        routed_failure.status_message
        + (f" — {routed_failure.issue_url}" if routed_failure.issue_url else ""),
    )
    return AbortedAgentCredentialFailure(status_code=routed_failure.status_code)


@dataclasses.dataclass(frozen=True)
class Continue:
    pass


@dataclasses.dataclass(frozen=True)
class AbortedHITL:
    issue_number: int


@dataclasses.dataclass(frozen=True)
class AbortedUsageLimit:
    reset_time: datetime | None = None
    provider: str | None = None
    raw_message: str | None = None
    account_label: str | None = None
    is_permanent: bool = False
    stage_key: str | None = None


@dataclasses.dataclass(frozen=True)
class AbortedModelNotAvailable:
    service: str | None = None
    model: str | None = None
    stage_key: str | None = None


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


@dataclasses.dataclass(frozen=True)
class AbortedHardApiError:
    status_code: int | None


@dataclasses.dataclass(frozen=True)
class AbortedAgentCredentialFailure:
    status_code: int | None


@dataclasses.dataclass(frozen=True)
class AbortedSetup:
    phase: str
    message: str
    command: str | None = None
    output: str | None = None


@dataclasses.dataclass(frozen=True)
class Done:
    improve_cap_reached: bool = False


@dataclasses.dataclass(frozen=True)
class AbortedOperatorActionable:
    op: str
    stderr: str
    attempt_count: int


@dataclasses.dataclass(frozen=True)
class MergeCloseFailure:
    filed_issue_numbers: list[int]


IterationOutcome: TypeAlias = (
    Continue
    | Done
    | AbortedHITL
    | AbortedUsageLimit
    | AbortedModelNotAvailable
    | NoCandidate
    | AbortedAgentFailure
    | AbortedTimeout
    | AbortedHardApiError
    | AbortedAgentCredentialFailure
    | AbortedSetup
    | AbortedOperatorActionable
    | MergeCloseFailure
)


async def _run_implement_and_merge(
    issues: list[dict],
    deps: Deps,
    sha: str | None,
) -> IterationOutcome:
    token = CancellationToken()
    async with status_row(
        deps.status_display,
        "Implement",
        kind="phase",
        must_close=True,
        initial_phase="Running",
    ) as row:
        impl_result = await implement_phase(issues, deps, sha, token=token)

        if impl_result.usage_limit_hit:
            row.close("finished")
            return AbortedUsageLimit(
                reset_time=impl_result.usage_limit_reset_time,
                provider=impl_result.usage_limit_provider,
                raw_message=impl_result.usage_limit_raw_message,
                account_label=impl_result.usage_limit_account_label,
                is_permanent=impl_result.usage_limit_is_permanent,
                stage_key="implement",
            )

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

    merge_result = await merge_phase(completed, deps)
    if merge_result.preflight_blocker is not None:
        return await _handle_preflight_outcome(merge_result.preflight_blocker, deps)
    if merge_result.close_failure_issue_numbers:
        return MergeCloseFailure(
            filed_issue_numbers=merge_result.close_failure_issue_numbers
        )
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
    afk_issue = deps.github_svc.get_issue(result.issue_number)
    return await _run_implement_and_merge([afk_issue], deps, result.sha)


async def run_iteration(deps: Deps) -> IterationOutcome:
    try:
        # ── Fetch issues ─────────────────────────────────────────────────────
        open_issues = deps.github_svc.get_open_issues(deps.cfg.issue_label)
        prepared_issue_set = prepare_planning_issue_set(open_issues, deps.cfg)
        prepared_open_issues = list(prepared_issue_set.prepared_issues)
        all_open_issues = deps.github_svc.get_all_open_issues_lightweight()

        in_flight = select_in_flight_issues(
            prepared_open_issues, repo_root=deps.repo_root, git_svc=deps.git_svc
        )

        # ── (Improve) — runs when idle: no open issues, no in-flight ────────
        if not open_issues and not in_flight:
            if (
                deps.improve_mode is not None
                and not (deps.improve_mode == "until_sleep" and deps.slept_once)
                and not (
                    deps.cfg.improve_max is not None
                    and deps.improve_dispatched_count >= deps.cfg.improve_max
                )
            ):
                improve_result = await improve_phase(deps)
                deps.improve_dispatched_count += 1
                if isinstance(improve_result, ImproveNoCandidate):
                    return NoCandidate()
                if isinstance(improve_result, (PreflightHITL, PreflightAFK)):
                    return await _handle_preflight_outcome(improve_result, deps)
                # ImproveContinue: re-fetch issues after improve filed new ones
                open_issues = deps.github_svc.get_open_issues(deps.cfg.issue_label)
                prepared_issue_set = prepare_planning_issue_set(open_issues, deps.cfg)
                prepared_open_issues = list(prepared_issue_set.prepared_issues)
                all_open_issues = deps.github_svc.get_all_open_issues_lightweight()
                if not open_issues:
                    return Continue()
                in_flight = select_in_flight_issues(
                    prepared_open_issues,
                    repo_root=deps.repo_root,
                    git_svc=deps.git_svc,
                )
            else:
                cap_hit = (
                    deps.cfg.improve_max is not None
                    and deps.improve_dispatched_count >= deps.cfg.improve_max
                    and deps.improve_mode is not None
                )
                return Done(improve_cap_reached=cap_hit)

        # ── Plan ─────────────────────────────────────────────────────────────
        plan_result = await planning_phase(
            deps,
            open_issues,
            all_open_issues,
            prepared_issue_set=prepared_issue_set,
            in_flight=in_flight,
        )
        if isinstance(plan_result, AllBlocked):
            return Done()
        if isinstance(plan_result, (PreflightHITL, PreflightAFK)):
            return await _handle_preflight_outcome(plan_result, deps)

        # ── Implement ────────────────────────────────────────────────────────
        return await _run_implement_and_merge(plan_result.issues, deps, plan_result.sha)

    except AgentFailedError as err:
        issue_number: int | None = None
        if deps.cfg.diagnose_on_failure:
            try:
                mount_decision = decide_diagnostic_mount_dispatch(
                    repo_root=deps.repo_root,
                    mount_path=err.worktree_path,
                    caller="Failure Report Agent",
                    diagnostic_role=AgentRole.FAILURE_REPORT.value,
                    role_name=err.role_value,
                    original_failure_summary=(
                        f"Agent role {err.role_value!r} failed in worktree "
                        f"{err.worktree_path}."
                    ),
                    github_svc=deps.github_svc,
                )
                if isinstance(mount_decision, DiagnosticMountFallbackIssue):
                    issue_number = mount_decision.issue_number
                    return AbortedAgentFailure(
                        failed_role=err.role_value, issue_number=issue_number
                    )

                raw_evidence_path = getattr(err, "agent_invocation_log_path", None)
                copied_evidence = _copy_invocation_log_to_evidence_area(
                    worktree_path=err.worktree_path,
                    source=raw_evidence_path,
                )
                if copied_evidence is not None:
                    err.agent_invocation_log_path = _evidence_relative_path()
                else:
                    err.agent_invocation_log_path = ""

                result = await deps.agent_runner.run(
                    RunRequest(
                        name="Failure Report Agent",
                        prompt=build_prompt_invocation(
                            PromptTemplate.FAILURE_REPORT,
                            build_failure_report_scope_args(err),
                        ),
                        mount_path=err.worktree_path,
                        role=AgentRole.FAILURE_REPORT,
                        service=deps.cfg.preflight_issue_override.service,
                        status_display=deps.status_display,
                    )
                )
                if isinstance(result, IssueOutput):
                    issue_number = result.number
            except AgentCredentialFailureError as report_err:
                routed_result = _route_and_abort_agent_credential_failure(
                    report_err, deps
                )
                assert routed_result is not None
                return routed_result
            except Exception as report_err:
                deps.status_display.print(
                    "Failure Report",
                    "Failure-Report agent crashed — no issue filed",
                    "warning",
                )
                deps.logger.log_internal_error(
                    f"Failure-Report agent crashed (original failure: role={err.role_value})",
                    report_err,
                    cause=err,
                )
        return AbortedAgentFailure(
            failed_role=err.role_value, issue_number=issue_number
        )
    except UsageLimitError as err:
        if (
            err.raw_message is not None
            and not err.is_permanent
            and err.raw_message not in _FILED_USAGE_LIMIT_RAW_MESSAGES
        ):
            _FILED_USAGE_LIMIT_RAW_MESSAGES.add(err.raw_message)
            provider = err.provider or "claude"
            title = f"[pycastle] failed to parse usage-limit reset time ({provider})"
            body = f"## Failed message\n\n```\n{err.raw_message}\n```\n\nProvider: {provider}; failure: usage-limit reset time parse failure\n"
            auto_file_issue(title, body, BUG_REPORT_LABEL_LIST, cfg=deps.cfg)
        return AbortedUsageLimit(
            reset_time=err.reset_time,
            provider=err.provider,
            raw_message=err.raw_message,
            account_label=err.account_label,
            is_permanent=err.is_permanent,
            stage_key=err.stage_key,
        )
    except ModelNotAvailableError as err:
        return AbortedModelNotAvailable(
            service=err.service,
            model=err.model,
            stage_key=err.stage_key,
        )
    except AgentTimeoutError as err:
        return AbortedTimeout(
            failed_role=err.role_value,
            worktree_path=err.worktree_path or deps.repo_root,
        )
    except OperatorActionableGitError as err:
        return AbortedOperatorActionable(
            op=err.op,
            stderr=err.stderr,
            attempt_count=err.attempt_count,
        )
    except TransientAgentError:
        return Continue()
    except HardAgentError as err:
        raw = err.args[0] if err.args else ""
        service_name = getattr(err, "service_name", "claude") or "claude"
        routed_result = _route_and_abort_agent_credential_failure(err, deps)
        if routed_result is not None:
            return routed_result

        effective_status_code = _extract_hard_error_status_code(raw, getattr(err, "status_code", None))
        error_text = _extract_legacy_hard_error_text(raw)
        first_line = next(iter(error_text.splitlines()), "") or str(err) or "<unknown>"
        service_label = {
            "claude": "Claude",
            "codex": "Codex",
            "opencode": "OpenCode",
        }.get(service_name, service_name)
        title = f"[pycastle] {service_label} API {effective_status_code}: {first_line}"
        body = (
            f"## Raw result envelope\n\n```json\n{raw}\n```\n\n"
            f"Status: {effective_status_code}\n"
            f"Agent: {err.caller or '<unknown>'}\n"
            f"Service: {service_name}\n"
        )
        url = auto_file_issue(title, body, BUG_REPORT_LABEL_LIST, cfg=deps.cfg)
        status_code_str = (
            str(effective_status_code)
            if effective_status_code is not None
            else "no status"
        )
        deps.status_display.print(
            err.caller,
            f"hard API error: status {status_code_str}" + (f" — {url}" if url else ""),
        )
        return AbortedHardApiError(status_code=effective_status_code)
    except SetupPhaseError as err:
        return AbortedSetup(
            phase=err.phase,
            message=str(err),
            command=err.command,
            output=err.output,
        )
