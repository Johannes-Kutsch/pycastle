import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from ..agents.output_protocol import AgentRole, IssueOutput
from ..agents.result import CancellationToken
from ..agents.runner import RunRequest
from ..bug_reporter import BUG_REPORT_LABEL_LIST, auto_file_issue
from ..errors import (
    AgentFailedError,
    AgentTimeoutError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
)
from ..services import OperatorActionableGitError
from ..prompts.pipeline import PromptTemplate
from ..infrastructure.worktree import worktree_name_for_branch, worktree_path
from ..session import any_role_dir_present
from ._deps import Deps
from ._rows import StatusRow as StatusRow
from ._rows import status_row as status_row
from .implement import branch_for, implement_phase
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


@dataclasses.dataclass(frozen=True)
class Continue:
    pass


@dataclasses.dataclass(frozen=True)
class AbortedHITL:
    issue_number: int


@dataclasses.dataclass(frozen=True)
class AbortedUsageLimit:
    reset_time: datetime | None = None


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
    | AbortedOperatorActionable
)


def _is_in_flight(issue: dict, deps: Deps) -> bool:
    branch = branch_for(issue["number"])
    wt_path = worktree_path(worktree_name_for_branch(branch), deps)
    if any_role_dir_present(wt_path):
        return True
    if not deps.git_svc.verify_ref_exists(branch, deps.repo_root):
        return False
    return deps.git_svc.branch_has_commits_ahead_of_merge_base(deps.repo_root, branch)


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
            return AbortedUsageLimit(reset_time=impl_result.usage_limit_reset_time)

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

    await merge_phase(completed, deps)
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
        open_issues = strip_stale_blocker_refs(
            deps.github_svc.get_open_issues(deps.cfg.issue_label)
        )
        all_open_issues = deps.github_svc.get_all_open_issues_lightweight()

        in_flight = [i for i in open_issues if _is_in_flight(i, deps)]

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
                in_flight = [i for i in open_issues if _is_in_flight(i, deps)]
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

        issues: list[dict] = plan_result.issues

        # ── Implement ────────────────────────────────────────────────────────
        return await _run_implement_and_merge(issues, deps, plan_result.sha)

    except AgentFailedError as err:
        issue_number: int | None = None
        if deps.cfg.diagnose_on_failure:
            result = await deps.agent_runner.run(
                RunRequest(
                    name="Failure Report Agent",
                    template=PromptTemplate.FAILURE_REPORT,
                    mount_path=err.worktree_path,
                    role=AgentRole.FAILURE_REPORT,
                    scope_args={
                        "FAILED_ROLE": err.role_value,
                        "SESSION_DIR": err.session_dir,
                        "FAILURE_CLASS": err.failure_class,
                    },
                    status_display=deps.status_display,
                )
            )
            if isinstance(result, IssueOutput):
                issue_number = result.number
        return AbortedAgentFailure(
            failed_role=err.role_value, issue_number=issue_number
        )
    except UsageLimitError as err:
        if (
            err.raw_message is not None
            and err.raw_message not in _FILED_USAGE_LIMIT_RAW_MESSAGES
        ):
            _FILED_USAGE_LIMIT_RAW_MESSAGES.add(err.raw_message)
            provider = err.provider or "claude"
            title = f"[pycastle] failed to parse usage-limit reset time ({provider})"
            body = f"## Failed message\n\n```\n{err.raw_message}\n```\n\nProvider: {provider}; failure: usage-limit reset time parse failure\n"
            auto_file_issue(title, body, BUG_REPORT_LABEL_LIST, cfg=deps.cfg)
        return AbortedUsageLimit(reset_time=err.reset_time)
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
        error_text = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("result"):
                error_text = str(parsed["result"])
        except (json.JSONDecodeError, TypeError):
            pass
        first_line = next(iter(error_text.splitlines()), "") or str(err) or "<unknown>"
        title = f"[pycastle] Claude API {err.status_code}: {first_line}"
        body = (
            f"## Raw result envelope\n\n```json\n{raw}\n```\n\n"
            f"Status: {err.status_code}\n"
            f"Agent: {err.caller or '<unknown>'}\n"
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
