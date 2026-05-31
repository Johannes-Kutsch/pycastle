from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..agents.output_protocol import (
    AgentOutput,
    AgentRole,
    IssueOutput,
    NoCandidateOutput,
)
from ..agents.runner import AgentRunnerProtocol, RunRequest
from ..config import Config
from ..prompts.pipeline import PromptTemplate, Scope, build_issue_scope_args
from ..services import GitService, ServiceRegistry
from ..services.github_service import GithubService
from ..session import RoleSession
from ..session.provider_session_state import (
    has_exact_provider_transcript_for_selected_service,
)
from ..display.status_display import StatusDisplay
from ..infrastructure.worktree import managed_worktree
from ._rows import status_row
from .preflight import PreflightAFK, PreflightCache, PreflightHITL


IMPROVE_SANDBOX = "pycastle/improve-sandbox"


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


@dataclass(frozen=True)
class Step:
    prompt_key: str
    cfg: _PhaseConfig
    send_role_prompt_on_resume: bool
    scope_args: dict[str, str]


class ImprovePhaseDriver:
    """State machine for the improve pipeline phases.

    Construction is side-effect-free; start() performs the first disk read.
    """

    _PROGRESS_FILE = "_phase_progress"
    _IN_FLIGHT_FILE = "_phase_in_flight"
    _VALID_PHASE_IDS = frozenset(
        {"01-scan:picked", "01-scan:no-candidate", "02-prd", "03-issues", "04-report"}
    )

    def __init__(
        self, role_session_dir: Path, short_sid: str, no_candidate_report: bool
    ) -> None:
        self._dir = role_session_dir
        self._progress_file = role_session_dir / self._PROGRESS_FILE
        self._in_flight_file = role_session_dir / self._IN_FLIGHT_FILE
        self._short_sid = short_sid
        self._no_candidate_report = no_candidate_report
        self._last_id: str | None = None
        self._prd_number: int | None = None

    def _load(self) -> tuple[str | None, str | None]:
        try:
            value = self._progress_file.read_text(encoding="utf-8").strip()
            last_id: str | None = value if value in self._VALID_PHASE_IDS else None
        except OSError:
            last_id = None

        in_flight_id: str | None = (
            self._in_flight_file.read_text(encoding="utf-8").strip()
            if self._in_flight_file.is_file()
            else None
        )

        # Orphan-reset: process restarted after phase 02 wrote progress but
        # before phase 03 recorded its in-flight marker. The only recoverable
        # phase-02 states are a true mid-phase retry ("02-prd") and a phase-03
        # continuation ("03-issues"). Any other state lost the in-memory
        # prd_number, so restart from phase 01 (leaves a dead PRD on GitHub
        # with no label).
        if last_id == "02-prd" and in_flight_id not in {"02-prd", "03-issues"}:
            self._progress_file.unlink(missing_ok=True)
            return None, None

        return last_id, in_flight_id

    def _next_prompt_key(self, last_id: str | None) -> str | None:
        if last_id is None:
            return "01-scan.md"
        if last_id == "01-scan:picked":
            return "02-prd.md"
        if last_id == "01-scan:no-candidate":
            return "04-no-candidate-report.md" if self._no_candidate_report else None
        if last_id == "02-prd":
            return "03-issues.md"
        return None

    def _resume_prompt_key(
        self, last_id: str | None, in_flight_id: str | None
    ) -> str | None:
        if last_id == "02-prd" and in_flight_id == "02-prd":
            return "02-prd.md"
        return self._next_prompt_key(last_id)

    def _compute_phase_id(self, prompt_key: str, output: AgentOutput) -> str:
        if prompt_key == "01-scan.md":
            return (
                "01-scan:no-candidate"
                if isinstance(output, NoCandidateOutput)
                else "01-scan:picked"
            )
        return {
            "02-prd.md": "02-prd",
            "03-issues.md": "03-issues",
            "04-no-candidate-report.md": "04-report",
        }.get(prompt_key, prompt_key)

    def _make_step(
        self, prompt_key: str, last_id: str | None, in_flight_id: str | None
    ) -> Step:
        phase = _PHASES[prompt_key]
        phase_key = prompt_key.removesuffix(".md")
        is_mid_phase_retry = in_flight_id == phase_key
        send_role_prompt_on_resume = last_id is not None and not is_mid_phase_retry

        if phase.template.scope in (Scope.IMPROVE_SESSION, Scope.IMPROVE_ISSUES):
            partial_scope_args: dict[str, str] = {"IMPROVE_SHORT_SID": self._short_sid}
        else:
            partial_scope_args = {}

        return Step(
            prompt_key=prompt_key,
            cfg=phase,
            send_role_prompt_on_resume=send_role_prompt_on_resume,
            scope_args=partial_scope_args,
        )

    def _write_in_flight(self, prompt_key: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._in_flight_file.write_text(
            prompt_key.removesuffix(".md"), encoding="utf-8"
        )

    def start(self) -> "Step | None":
        last_id, in_flight_id = self._load()
        self._last_id = last_id
        prompt_key = self._resume_prompt_key(last_id, in_flight_id)
        if prompt_key is None:
            return None
        step = self._make_step(prompt_key, last_id, in_flight_id)
        self._write_in_flight(prompt_key)
        return step

    def next(self) -> "Step | None":
        prompt_key = self._next_prompt_key(self._last_id)
        if prompt_key is None:
            return None
        step = self._make_step(prompt_key, self._last_id, None)
        self._write_in_flight(prompt_key)
        return step

    def record_outcome(self, step: "Step", output: AgentOutput) -> None:
        completed_id = self._compute_phase_id(step.prompt_key, output)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._progress_file.write_text(completed_id, encoding="utf-8")
        self._in_flight_file.unlink(missing_ok=True)
        self._last_id = completed_id

        if step.prompt_key == "02-prd.md" and isinstance(output, IssueOutput):
            self._prd_number = output.number

    @property
    def prd_number(self) -> int | None:
        return self._prd_number

    @property
    def last_id(self) -> str | None:
        return self._last_id


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
    service_registry: ServiceRegistry | None
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
    async with status_row(
        deps.status_display,
        phase_label,
        kind="phase",
        must_close=True,
        initial_phase="Running",
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
            driver = ImprovePhaseDriver(
                role_session_dir, short_sid, deps.cfg.diagnose_on_failure
            )

            step = driver.start()
            if (
                step is not None
                and step.prompt_key == "02-prd.md"
                and step.send_role_prompt_on_resume
            ):
                service_name = deps.cfg.improve_override.service
                has_exact_main_transcript = (
                    has_exact_provider_transcript_for_selected_service(
                        worktree=sandbox_path,
                        role=AgentRole.IMPROVE,
                        namespace="main",
                        registry=deps.service_registry,
                        service_name=service_name,
                    )
                )
                if not has_exact_main_transcript:
                    deps.status_display.print(
                        "Improve",
                        "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
                    )
                    role_session.discard()
                    row.close("restarting from phase 1")
                    return ImproveContinue()

            while step is not None:
                if step.cfg.template.scope is Scope.IMPROVE_ISSUES:
                    scope_args = _build_issues_scope_args(
                        short_sid, driver.prd_number, deps.github_svc
                    )
                else:
                    scope_args = {**step.scope_args}
                output = await deps.agent_runner.run(
                    RunRequest(
                        name=step.cfg.display_name,
                        template=step.cfg.template,
                        mount_path=sandbox_path,
                        role=AgentRole.IMPROVE,
                        scope_args=scope_args,
                        model=deps.cfg.improve_override.model,
                        effort=deps.cfg.improve_override.effort,
                        service=deps.cfg.improve_override.service,
                        stage="improve-sandbox",
                        status_display=deps.status_display,
                        work_body=step.cfg.display_body,
                        send_role_prompt_on_resume=step.send_role_prompt_on_resume,
                        session_namespace=step.cfg.namespace,
                    )
                )
                driver.record_outcome(step, output)
                step = driver.next()

            no_candidate = driver.last_id in {"01-scan:no-candidate", "04-report"}
            role_session.discard()

        row.close("finished")
    return ImproveNoCandidate() if no_candidate else ImproveContinue()
