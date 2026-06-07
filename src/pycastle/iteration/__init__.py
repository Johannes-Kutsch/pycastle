import dataclasses
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from ..agents.output_protocol import AgentRole, IssueOutput
from ..agents.result import CancellationToken
from ..agents.runner import RunRequest
from ..bug_reporter import (
    BUG_REPORT_LABEL_LIST,
    auto_file_issue,
    file_agent_credential_failure_issue,
)
from ..errors import (
    AgentFailedError,
    AgentTimeoutError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from ..services import OperatorActionableGitError
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
from .preflight import (
    PreflightAFK,
    PreflightCache as PreflightCache,
    PreflightHITL,
    strip_stale_blocker_refs,
)

_FILED_USAGE_LIMIT_RAW_MESSAGES: set[str] = set()


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
    except (json.JSONDecodeError, TypeError):
        pass
    return error_text


_SHARED_AGENT_CREDENTIAL_FAILURE_CLASSIFICATION = (
    "operator_actionable_agent_credential_failure"
)


def _is_codex_refresh_token_reused_signature(text: str) -> bool:
    if "refresh_token_reused" in text:
        return True
    lowered = text.lower()
    return (
        "access token could not be refreshed" in lowered
        and "refresh token was already used" in lowered
    )


def _is_codex_missing_host_auth_signature(text: str) -> bool:
    lowered = text.lower()
    return (
        "codex authentication missing" in lowered
        and "codex login" in lowered
        and "host" in lowered
    )


def _is_claude_subscription_access_denial(text: str) -> bool:
    return "disabled claude subscription access for claude code" in text.lower()


def _is_opencode_invalid_api_key_signature(text: str) -> bool:
    lowered = text.lower()
    return "invalid api key" in lowered or "invalid_api_key" in lowered


def _shared_credential_failure_remediation(
    *,
    service_name: str,
    raw: str,
    rendered_observations: tuple[tuple[str, str], ...],
) -> str:
    haystacks = tuple(text for _, text in rendered_observations) + (raw,)
    if service_name == "codex":
        if any(_is_codex_refresh_token_reused_signature(text) for text in haystacks):
            return "Run `codex login` on the host to reseed credentials."
        if any(_is_codex_missing_host_auth_signature(text) for text in haystacks):
            return "Run `codex login` on the host to seed Codex credentials before dispatch."
    if service_name == "claude" and any(
        _is_claude_subscription_access_denial(text) for text in haystacks
    ):
        return (
            "Restore Claude Code subscription access or use a token/account with "
            "access and rerun pycastle."
        )
    if service_name == "opencode" and any(
        _is_opencode_invalid_api_key_signature(text) for text in haystacks
    ):
        return "Update the configured OpenCode API key and rerun pycastle."
    return "Repair the local agent credentials/account access."


def _classify_agent_credential_failure(
    *,
    service_name: str,
    status_code: int | None,
    classification: str | None,
    raw: str,
    observations: tuple,
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    if classification == _SHARED_AGENT_CREDENTIAL_FAILURE_CLASSIFICATION:
        rendered = tuple(
            (obs.source_stream, obs.raw_provider_text) for obs in observations
        ) or (("raw error", raw),)
        return (
            _shared_credential_failure_remediation(
                service_name=service_name,
                raw=raw,
                rendered_observations=rendered,
            ),
            rendered,
        )
    if service_name != "codex" or status_code != 401:
        return None

    rendered_observations = tuple(
        (obs.source_stream, obs.raw_provider_text) for obs in observations
    )
    haystacks = tuple(text for _, text in rendered_observations) + (raw,)
    if any(_is_codex_refresh_token_reused_signature(text) for text in haystacks):
        return (
            "Run `codex login` on the host to reseed credentials.",
            rendered_observations or (("raw error", raw),),
        )
    if any(_is_codex_missing_host_auth_signature(text) for text in haystacks):
        return (
            "Run `codex login` on the host to seed Codex credentials before dispatch.",
            rendered_observations or (("raw error", raw),),
        )
    return None


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


IterationOutcome: TypeAlias = (
    Continue
    | Done
    | AbortedHITL
    | AbortedUsageLimit
    | NoCandidate
    | AbortedAgentFailure
    | AbortedTimeout
    | AbortedHardApiError
    | AbortedAgentCredentialFailure
    | AbortedSetup
    | AbortedOperatorActionable
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
    return Continue()


def _issues_with_readiness(
    issues: list[dict], readiness_by_number: Mapping[int, object]
) -> list[dict]:
    return [
        (
            {**issue, "readiness": readiness}
            if (readiness := readiness_by_number.get(issue["number"])) is not None
            else issue
        )
        for issue in issues
    ]


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
        open_issues = strip_stale_blocker_refs(
            deps.github_svc.get_open_issues(deps.cfg.issue_label)
        )
        all_open_issues = deps.github_svc.get_all_open_issues_lightweight()

        in_flight = select_in_flight_issues(
            open_issues, repo_root=deps.repo_root, git_svc=deps.git_svc
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
                open_issues = strip_stale_blocker_refs(
                    deps.github_svc.get_open_issues(deps.cfg.issue_label)
                )
                all_open_issues = deps.github_svc.get_all_open_issues_lightweight()
                if not open_issues:
                    return Continue()
                in_flight = select_in_flight_issues(
                    open_issues, repo_root=deps.repo_root, git_svc=deps.git_svc
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
            deps, open_issues, all_open_issues, in_flight=in_flight
        )
        if isinstance(plan_result, AllBlocked):
            return Done()
        if isinstance(plan_result, (PreflightHITL, PreflightAFK)):
            return await _handle_preflight_outcome(plan_result, deps)

        issues = _issues_with_readiness(
            plan_result.issues, plan_result.readiness_by_number
        )

        # ── Implement ────────────────────────────────────────────────────────
        return await _run_implement_and_merge(issues, deps, plan_result.sha)

    except AgentFailedError as err:
        issue_number: int | None = None
        if deps.cfg.diagnose_on_failure:
            try:
                result = await deps.agent_runner.run(
                    RunRequest(
                        name="Failure Report Agent",
                        template=PromptTemplate.FAILURE_REPORT,
                        mount_path=err.worktree_path,
                        role=AgentRole.FAILURE_REPORT,
                        scope_args=build_failure_report_scope_args(err),
                        service=deps.cfg.preflight_issue_override.service,
                        status_display=deps.status_display,
                    )
                )
                if isinstance(result, IssueOutput):
                    issue_number = result.number
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
        credential_failure = _classify_agent_credential_failure(
            service_name=service_name,
            status_code=err.status_code,
            classification=getattr(err, "classification", None),
            raw=raw,
            observations=getattr(err, "observations", ()),
        )
        if credential_failure is not None:
            remediation, rendered_observations = credential_failure
            url = file_agent_credential_failure_issue(
                service_name=service_name,
                role_name=err.caller,
                status_code=err.status_code,
                raw_result_envelope=raw,
                remediation=remediation,
                observations=rendered_observations,
                github_svc=deps.github_svc,
            )
            status_code_str = (
                str(err.status_code) if err.status_code is not None else "no status"
            )
            status_message = (
                "operator-actionable agent credential failure: "
                f"status {status_code_str}"
            )
            if url is None:
                local_evidence = (
                    rendered_observations[0][1] if rendered_observations else raw
                )
                status_message = (
                    "operator-actionable agent credential failure: "
                    f"{remediation} Evidence: {local_evidence}"
                )
            deps.status_display.print(
                err.caller,
                status_message + (f" — {url}" if url else ""),
            )
            return AbortedAgentCredentialFailure(status_code=err.status_code)

        error_text = _extract_legacy_hard_error_text(raw)
        first_line = next(iter(error_text.splitlines()), "") or str(err) or "<unknown>"
        service_label = {
            "claude": "Claude",
            "codex": "Codex",
            "opencode": "OpenCode",
        }.get(service_name, service_name)
        title = f"[pycastle] {service_label} API {err.status_code}: {first_line}"
        body = (
            f"## Raw result envelope\n\n```json\n{raw}\n```\n\n"
            f"Status: {err.status_code}\n"
            f"Agent: {err.caller or '<unknown>'}\n"
            f"Service: {service_name}\n"
        )
        url = auto_file_issue(title, body, BUG_REPORT_LABEL_LIST, cfg=deps.cfg)
        status_code_str = (
            str(err.status_code) if err.status_code is not None else "no status"
        )
        deps.status_display.print(
            err.caller,
            f"hard API error: status {status_code_str}" + (f" — {url}" if url else ""),
        )
        return AbortedHardApiError(status_code=err.status_code)
    except SetupPhaseError as err:
        return AbortedSetup(
            phase=err.phase,
            message=str(err),
            command=err.command,
            output=err.output,
        )
