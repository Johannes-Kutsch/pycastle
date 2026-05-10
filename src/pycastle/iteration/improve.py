import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..agent_output_protocol import (
    AgentOutput,
    AgentRole,
    FailedOutput,
    IssueOutput,
    NoCandidateOutput,
)
from ..agent_runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..errors import AgentFailedError
from ..prompt_pipeline import PromptTemplate, Scope, build_issue_scope_args
from ..services import GitService
from ..services.github_service import GithubService
from ..session_resume import RoleSession
from ..status_display import StatusDisplay
from ..worktree import managed_worktree
from ._rows import phase_row
from .preflight import PreflightAFK, PreflightCache, PreflightHITL


IMPROVE_SANDBOX = "pycastle/improve-sandbox"
_PHASE_PROGRESS_FILE = "_phase_progress"
_PHASE_IN_FLIGHT_FILE = "_phase_in_flight"


@dataclass(frozen=True)
class _PhaseConfig:
    template: PromptTemplate
    namespace: str
    display_name: str
    display_body: str


_PHASES: dict[str, _PhaseConfig] = {
    "01-scan.md": _PhaseConfig(
        template=PromptTemplate.IMPROVE_SCAN,
        namespace="main",
        display_name="Scan Agent",
        display_body="picking an improvement",
    ),
    "02-prd.md": _PhaseConfig(
        template=PromptTemplate.IMPROVE_PRD,
        namespace="main",
        display_name="PRD Agent",
        display_body="writing PRD",
    ),
    "03-issues.md": _PhaseConfig(
        template=PromptTemplate.IMPROVE_ISSUES,
        namespace="issues",
        display_name="Slice Agent",
        display_body="filing sub-issues",
    ),
    "04-no-candidate-report.md": _PhaseConfig(
        template=PromptTemplate.IMPROVE_NO_CANDIDATE,
        namespace="main",
        display_name="Rejection Report Agent",
        display_body="filing no-candidate report",
    ),
}
_VALID_PHASE_IDS = frozenset(
    {"01-scan:picked", "01-scan:no-candidate", "02-prd", "03-issues", "04-report"}
)


def next_prompt(
    last_completed_id: str | None, *, no_candidate_report: bool
) -> str | None:
    """Pure transition function: last completed phase ID → next prompt filename."""
    if last_completed_id is None:
        return "01-scan.md"
    if last_completed_id == "01-scan:picked":
        return "02-prd.md"
    if last_completed_id == "01-scan:no-candidate":
        return "04-no-candidate-report.md" if no_candidate_report else None
    if last_completed_id == "02-prd":
        return "03-issues.md"
    # 03-issues, 04-report, or unrecognised → terminal
    return None


def _phase_id(prompt_name: str, output: AgentOutput) -> str:
    if prompt_name == "01-scan.md":
        return (
            "01-scan:no-candidate"
            if isinstance(output, NoCandidateOutput)
            else "01-scan:picked"
        )
    return {
        "02-prd.md": "02-prd",
        "03-issues.md": "03-issues",
        "04-no-candidate-report.md": "04-report",
    }.get(prompt_name, prompt_name)


def _read_progress(progress_file: Path) -> str | None:
    try:
        value = progress_file.read_text(encoding="utf-8").strip()
        return value if value in _VALID_PHASE_IDS else None
    except OSError:
        return None


@dataclass(frozen=True)
class ImproveNoCandidate:
    pass


@dataclass(frozen=True)
class ImproveContinue:
    pass


class _ImproveDeps(Protocol):
    cfg: Config
    status_display: StatusDisplay
    agent_runner: AgentRunnerProtocol
    repo_root: Path
    git_svc: GitService
    github_svc: GithubService
    preflight_cache: PreflightCache
    improve_dispatched_count: int


def _build_issues_scope_args(
    short_sid: str,
    prd_number: int | None,
    github_svc: GithubService,
) -> dict[str, str]:
    if prd_number is None:
        issue: dict = {"number": "", "title": "", "body": "", "comments": []}
    else:
        issue = {
            **github_svc.get_issue(prd_number),
            "comments": github_svc.get_issue_comments(prd_number),
        }
    return build_issue_scope_args(
        issue, extra_scope_args={"IMPROVE_SHORT_SID": short_sid}
    )


async def improve_phase(
    deps: _ImproveDeps,
) -> ImproveNoCandidate | ImproveContinue | PreflightHITL | PreflightAFK:
    """Run the improve pipeline."""
    if deps.cfg.improve_max is not None:
        phase_label = (
            f"Improve ({deps.improve_dispatched_count}/{deps.cfg.improve_max})"
        )
    else:
        phase_label = "Improve"
    async with phase_row(
        deps.status_display, phase_label, initial_phase="Running"
    ) as row:
        verdict = await deps.preflight_cache.get_safe_sha(deps)
        if isinstance(verdict, (PreflightHITL, PreflightAFK)):
            row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
            return verdict

        async with managed_worktree(
            "improve-sandbox",
            branch=IMPROVE_SANDBOX,
            sha=verdict.sha,
            delete_branch_on_teardown=True,
            deps=deps,
        ) as sandbox_path:
            role_session = RoleSession(sandbox_path, AgentRole.IMPROVE)
            short_sid = role_session.session_uuid().split("-")[0]
            role_session_dir = role_session.path
            progress_file = role_session_dir / _PHASE_PROGRESS_FILE
            in_flight_file = role_session_dir / _PHASE_IN_FLIGHT_FILE

            last_id = _read_progress(progress_file)
            in_flight_id = (
                in_flight_file.read_text(encoding="utf-8").strip()
                if in_flight_file.is_file()
                else None
            )

            # Orphan-reset: process restarted after phase 02 wrote progress but
            # before phase 03 recorded its in-flight marker.  The in-memory
            # prd_number is gone; there is no recovery path.  Clear progress and
            # restart from phase 01 (leaves a dead PRD on GitHub with no label).
            if last_id == "02-prd" and in_flight_id != "03-issues":
                progress_file.unlink(missing_ok=True)
                last_id = None
                in_flight_id = None

            prd_number: int | None = None
            prompt_name = next_prompt(
                last_id,
                no_candidate_report=deps.cfg.diagnose_on_failure,
            )
            while prompt_name is not None:
                phase = _PHASES[prompt_name]
                template = phase.template
                if template.scope is Scope.IMPROVE_SESSION:
                    scope_args: dict[str, str] = {"IMPROVE_SHORT_SID": short_sid}
                elif template.scope is Scope.IMPROVE_ISSUES:
                    scope_args = _build_issues_scope_args(
                        short_sid, prd_number, deps.github_svc
                    )
                else:
                    scope_args = {}
                phase_key = prompt_name.removesuffix(".md")
                is_mid_phase_retry = in_flight_id == phase_key
                role_session_dir.mkdir(parents=True, exist_ok=True)
                in_flight_file.write_text(phase_key, encoding="utf-8")
                output = await deps.agent_runner.run(
                    RunRequest(
                        name=phase.display_name,
                        template=template,
                        mount_path=sandbox_path,
                        role=AgentRole.IMPROVE,
                        skip_preflight=True,
                        scope_args=scope_args,
                        model=deps.cfg.improve_override.model,
                        effort=deps.cfg.improve_override.effort,
                        stage="improve-sandbox",
                        status_display=deps.status_display,
                        work_body=phase.display_body,
                        send_role_prompt_on_resume=last_id is not None
                        and not is_mid_phase_retry,
                        session_namespace=phase.namespace,
                    )
                )

                if isinstance(output, FailedOutput):
                    raise AgentFailedError(
                        role_value=AgentRole.IMPROVE.value,
                        worktree_path=sandbox_path,
                        namespace=phase.namespace,
                        failure_class=output.failure_class,
                    )
                if prompt_name == "02-prd.md" and isinstance(output, IssueOutput):
                    prd_number = output.number
                completed_id = _phase_id(prompt_name, output)
                role_session_dir.mkdir(parents=True, exist_ok=True)
                progress_file.write_text(completed_id, encoding="utf-8")
                in_flight_file.unlink(missing_ok=True)
                last_id = completed_id
                in_flight_id = None

                prompt_name = next_prompt(
                    completed_id,
                    no_candidate_report=deps.cfg.diagnose_on_failure,
                )

            no_candidate = last_id in {"01-scan:no-candidate", "04-report"}
            shutil.rmtree(role_session_dir, ignore_errors=True)

        row.close("finished")
    return ImproveNoCandidate() if no_candidate else ImproveContinue()
