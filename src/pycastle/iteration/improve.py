import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from pycastle.agents.output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    IssueOutput,
    NoCandidateOutput,
    ScanCandidateItem,
    ScanCandidatesOutput,
)
from pycastle.agents.runner import AgentRunnerProtocol, RunRequest
from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.errors import SetupPhaseError
from pycastle.infrastructure.worktree import (
    SandboxWorktreeIntent,
    reusable_sandbox_worktree,
    reusable_sandbox_worktree_identity,
)
from pycastle.iteration._fingerprint import prepare_fingerprint_gate
from pycastle.iteration._rows import StatusRowConfig, status_row
from pycastle.iteration.improve_filing import (
    _CandidateRecord,
    _load_record,
    _save_record,
)
from pycastle.iteration.improve_preparation import prepare_improve_step
from pycastle.iteration.preflight import PreflightAFK, PreflightCache, PreflightHITL
from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import compute_candidate_budget
from pycastle.runtime_session import session_uuid
from pycastle.services import GitService, ServiceRegistry
from pycastle.services.github_service import GithubService
from pycastle.session import RoleSession, has_exact_transcript_match

if TYPE_CHECKING:
    from pycastle.services.runtime_services import AgentService

IMPROVE_SANDBOX_INTENT = SandboxWorktreeIntent.IMPROVE
IMPROVE_SANDBOX = f"pycastle/{IMPROVE_SANDBOX_INTENT.value}"


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
        namespace="main",
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
    fetch_recent_prd_titles: bool
    prd_number: int | None


class ImprovePhaseDriver:
    """State machine for the improve pipeline phases.

    Construction is side-effect-free; start() performs the first disk read.

    State is held in three role-level files:
    - _candidate_list: JSON with ordered scan candidates (and no_candidate flag).
    - _candidate_cursor: integer index of the candidate currently being processed.
    - _phase_in_flight: key of the phase currently executing (for mid-phase resumption).

    Per-candidate state lives in candidates/<idx>/_candidate_record (written by the
    filing pass and widened here to carry prd_number from the spec phase).
    """

    _CANDIDATE_LIST_FILE = "_candidate_list"
    _CANDIDATE_CURSOR_FILE = "_candidate_cursor"
    _IN_FLIGHT_FILE = "_phase_in_flight"

    def __init__(self, role_session_dir: Path, *, no_candidate_report: bool) -> None:
        self._dir = role_session_dir
        self._no_candidate_report = no_candidate_report
        self._candidates: list[ScanCandidateItem] | None = None
        self._no_candidate: bool = False
        self._cursor: int = 0
        self._prd_number: int | None = None

    # ── Disk I/O helpers ──────────────────────────────────────────────────────

    def _candidate_list_path(self) -> Path:
        return self._dir / self._CANDIDATE_LIST_FILE

    def _candidate_cursor_path(self) -> Path:
        return self._dir / self._CANDIDATE_CURSOR_FILE

    def _in_flight_path(self) -> Path:
        return self._dir / self._IN_FLIGHT_FILE

    def _candidate_dir(self, idx: int) -> Path:
        return self._dir / "candidates" / str(idx)

    def _load_state(self) -> tuple[list[ScanCandidateItem] | None, bool, int]:
        """Return (candidates, no_candidate, cursor). None candidates = scan not done."""
        list_path = self._candidate_list_path()
        if not list_path.is_file():
            return None, False, 0
        try:
            data = json.loads(list_path.read_text(encoding="utf-8"))
            no_candidate = data.get("no_candidate", False)
            candidates = [
                ScanCandidateItem(rank=c["rank"], title=c["title"])
                for c in data.get("candidates", [])
            ]
        except (KeyError, json.JSONDecodeError):
            return None, False, 0

        cursor = 0
        cursor_path = self._candidate_cursor_path()
        if cursor_path.is_file():
            with contextlib.suppress(ValueError, OSError):
                cursor = int(cursor_path.read_text(encoding="utf-8").strip())
        return candidates, no_candidate, cursor

    def _save_candidate_list(
        self, candidates: list[ScanCandidateItem], *, no_candidate: bool = False
    ) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        data: dict = {
            "candidates": [{"rank": c.rank, "title": c.title} for c in candidates],
        }
        if no_candidate:
            data["no_candidate"] = True
        self._candidate_list_path().write_text(json.dumps(data), encoding="utf-8")

    def _save_cursor(self, cursor: int) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._candidate_cursor_path().write_text(str(cursor), encoding="utf-8")

    def _write_in_flight(self, phase_key: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._in_flight_path().write_text(phase_key, encoding="utf-8")

    def _load_in_flight(self) -> str | None:
        p = self._in_flight_path()
        return p.read_text(encoding="utf-8").strip() if p.is_file() else None

    def _clear_in_flight(self) -> None:
        self._in_flight_path().unlink(missing_ok=True)

    def _load_candidate_record(self, idx: int) -> _CandidateRecord | None:
        return _load_record(self._candidate_dir(idx))

    def _write_prd_number(self, idx: int, prd_number: int | None) -> None:
        """Record that the spec (PRD) phase completed for candidate idx."""
        candidate_dir = self._candidate_dir(idx)
        record = _load_record(candidate_dir)
        if record is None:
            record = _CandidateRecord(
                spec_number=None,
                spec_database_id=None,
                spec_title="",
                filed_slices=[],
                labels_applied=False,
                prd_number=prd_number,
            )
        else:
            record.prd_number = prd_number
        _save_record(candidate_dir, record)

    # ── Step factories ────────────────────────────────────────────────────────

    def _make_scan_step(self, *, fetch_recent_prd_titles: bool) -> Step:
        return Step(
            prompt_key="01-scan.md",
            cfg=_PHASES["01-scan.md"],
            send_role_prompt_on_resume=False,
            fetch_recent_prd_titles=fetch_recent_prd_titles,
            prd_number=None,
        )

    def _make_prd_step(self, *, send_role_prompt_on_resume: bool) -> Step:
        return Step(
            prompt_key="02-prd.md",
            cfg=_PHASES["02-prd.md"],
            send_role_prompt_on_resume=send_role_prompt_on_resume,
            fetch_recent_prd_titles=True,
            prd_number=None,
        )

    def _make_issues_step(self, prd_number: int | None) -> Step:
        return Step(
            prompt_key="03-issues.md",
            cfg=_PHASES["03-issues.md"],
            send_role_prompt_on_resume=True,
            fetch_recent_prd_titles=False,
            prd_number=prd_number,
        )

    def _make_report_step(self, *, send_role_prompt_on_resume: bool) -> Step:
        return Step(
            prompt_key="04-no-candidate-report.md",
            cfg=_PHASES["04-no-candidate-report.md"],
            send_role_prompt_on_resume=send_role_prompt_on_resume,
            fetch_recent_prd_titles=True,
            prd_number=None,
        )

    # ── Core state resolution ─────────────────────────────────────────────────

    def _step_for_candidate(self, idx: int, *, from_start: bool) -> Step | None:
        """Return the next step for candidate at idx, or None if already complete."""
        record = self._load_candidate_record(idx)

        if record is not None and record.labels_applied:
            return None  # Candidate fully complete

        if record is None:
            # No record → spec (PRD) phase. Check in-flight for mid-PRD resume.
            in_flight = self._load_in_flight() if from_start else None
            is_mid_prd = in_flight == "02-prd"
            return self._make_prd_step(send_role_prompt_on_resume=not is_mid_prd)

        # Has record (prd_number or spec_number set) → slice (Issues) phase.
        prd_number = record.prd_number
        self._prd_number = prd_number
        in_flight = self._load_in_flight() if from_start else None
        is_mid_issues = in_flight == "03-issues"
        step = self._make_issues_step(prd_number)
        # For mid-issues resume, override send_role_prompt_on_resume to False.
        if is_mid_issues:
            step = Step(
                prompt_key=step.prompt_key,
                cfg=step.cfg,
                send_role_prompt_on_resume=False,
                fetch_recent_prd_titles=step.fetch_recent_prd_titles,
                prd_number=step.prd_number,
            )
        return step

    def _next_step_from_cursor(self, cursor: int, *, from_start: bool) -> Step | None:
        """Scan forward from cursor to find next candidate needing work."""
        candidates = self._candidates
        if candidates is None:
            return None
        while cursor < len(candidates):
            step = self._step_for_candidate(cursor, from_start=from_start)
            if step is not None:
                self._cursor = cursor
                return step
            # labels_applied=True: auto-advance cursor for this completed candidate.
            cursor += 1
            self._save_cursor(cursor)
        return None  # All candidates done

    # ── Public interface ──────────────────────────────────────────────────────

    def start(self) -> "Step | None":
        candidates, no_candidate, cursor = self._load_state()

        if candidates is None:
            # Scan not done → return scan step.
            in_flight = self._load_in_flight()
            is_mid_scan = in_flight == "01-scan"
            step = self._make_scan_step(fetch_recent_prd_titles=not is_mid_scan)
            self._write_in_flight("01-scan")
            return step

        self._candidates = candidates
        self._no_candidate = no_candidate
        self._cursor = cursor

        if no_candidate:
            if self._no_candidate_report and cursor == 0:
                in_flight = self._load_in_flight()
                is_mid_report = in_flight == "04-no-candidate-report"
                step = self._make_report_step(
                    send_role_prompt_on_resume=not is_mid_report
                )
                self._write_in_flight("04-no-candidate-report")
                return step
            return None

        next_step: Step | None = self._next_step_from_cursor(cursor, from_start=True)
        if next_step is not None:
            self._write_in_flight(next_step.prompt_key.removesuffix(".md"))
        return next_step

    def next(self) -> "Step | None":
        candidates = self._candidates
        if candidates is None:
            return None

        if self._no_candidate:
            if self._no_candidate_report and self._cursor == 0:
                step = self._make_report_step(send_role_prompt_on_resume=True)
                self._write_in_flight("04-no-candidate-report")
                return step
            return None

        next_step: Step | None = self._next_step_from_cursor(self._cursor, from_start=False)
        if next_step is not None:
            self._write_in_flight(next_step.prompt_key.removesuffix(".md"))
        return next_step

    def record_outcome(self, step: "Step", output: AgentOutput) -> None:
        if step.prompt_key == "01-scan.md":
            if isinstance(output, ScanCandidatesOutput):
                candidates = list(output.candidates)
                self._candidates = candidates
                self._no_candidate = False
                self._cursor = 0
                self._save_candidate_list(candidates)
                self._save_cursor(0)
            elif isinstance(output, NoCandidateOutput):
                self._candidates = []
                self._no_candidate = True
                self._cursor = 0
                self._save_candidate_list([], no_candidate=True)
                self._save_cursor(0)

        elif step.prompt_key == "02-prd.md":
            prd_number = output.number if isinstance(output, IssueOutput) else None
            self._prd_number = prd_number
            self._write_prd_number(self._cursor, prd_number)

        elif step.prompt_key == "03-issues.md":
            self._cursor += 1
            self._save_cursor(self._cursor)

        elif step.prompt_key == "04-no-candidate-report.md":
            # cursor=1 marks report done; checked in start() and next().
            self._cursor = 1
            self._save_cursor(1)

        self._clear_in_flight()

    @property
    def prd_number(self) -> int | None:
        return self._prd_number

    @property
    def no_candidate(self) -> bool:
        return self._no_candidate

    @property
    def all_candidates_complete(self) -> bool:
        if self._candidates is None:
            return False
        return self._cursor >= len(self._candidates)


@dataclass(frozen=True)
class ImproveNoCandidate:
    pass


@dataclass(frozen=True)
class ImproveContinue:
    completed: bool = True


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
        config=StatusRowConfig(initial_phase="Running"),
    ) as row:
        verdict = await deps.preflight_cache.get_safe_sha(deps)
        if isinstance(verdict, (PreflightHITL, PreflightAFK)):
            row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
            return verdict

        fingerprint = verdict.sha
        pre_sandbox_path = reusable_sandbox_worktree_identity(
            IMPROVE_SANDBOX_INTENT, deps.repo_root
        ).path
        prepare_fingerprint_gate(
            RoleSession(pre_sandbox_path, AgentRole.IMPROVE), fingerprint
        )

        async with reusable_sandbox_worktree(
            IMPROVE_SANDBOX_INTENT,
            sha=verdict.sha,
            deps=deps,
        ) as sandbox_path:
            role_session = RoleSession(sandbox_path, AgentRole.IMPROVE)
            short_sid = session_uuid(
                sandbox_path, AgentRole.IMPROVE.value, "main"
            ).split("-")[0]
            role_session_dir = role_session.path
            driver = ImprovePhaseDriver(
                role_session_dir, no_candidate_report=deps.cfg.diagnose_on_failure
            )

            step = driver.start()
            if (
                step is not None
                and step.prompt_key == "02-prd.md"
                and step.send_role_prompt_on_resume
            ):
                service_name = deps.cfg.improve_override.service
                service_registry = deps.service_registry
                service = (
                    service_registry[service_name]
                    if service_registry is not None
                    else None
                )
                has_exact_main_transcript = service is not None and (
                    has_exact_transcript_match(
                        worktree=sandbox_path,
                        role=AgentRole.IMPROVE,
                        session_namespace="main",
                        service=cast("AgentService", service),
                    )
                )
                if not has_exact_main_transcript:
                    deps.status_display.print(
                        "Improve",
                        "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
                    )
                    role_session.discard()
                    row.close("restarting from phase 1")
                    return ImproveContinue(completed=False)

            role_session.write_fingerprint(fingerprint)

            candidate_budget = compute_candidate_budget(
                candidates_per_scan=deps.cfg.improve_candidates_per_scan,
                improve_max=deps.cfg.improve_max,
                dispatched=deps.improve_dispatched_count,
            )
            while step is not None:
                prepared_step = prepare_improve_step(
                    step,
                    github_port=deps.github_svc,
                    short_sid=short_sid,
                    candidate_budget=candidate_budget,
                )
                mount_decision = decide_managed_worktree_mount(
                    repo_root=deps.repo_root,
                    mount_path=sandbox_path,
                    caller=prepared_step.name,
                    role=AgentRole.IMPROVE.value,
                )
                if isinstance(
                    mount_decision, ManagedWorktreeMountRejected
                ) and should_reject_managed_worktree_mount(mount_decision):
                    raise SetupPhaseError(
                        AgentRole.IMPROVE.value,
                        describe_managed_worktree_mount_rejection(mount_decision),
                    )
                output = await deps.agent_runner.run(
                    RunRequest(
                        name=prepared_step.name,
                        prompt=prepared_step.prompt,
                        mount_path=sandbox_path,
                        role=AgentRole.IMPROVE,
                        model=deps.cfg.improve_override.model,
                        effort=deps.cfg.improve_override.effort,
                        service=deps.cfg.improve_override.service,
                        stage="improve-sandbox",
                        status_display=deps.status_display,
                        work_body=prepared_step.work_body,
                        session_namespace=prepared_step.session_namespace,
                        preserve_session_on_completion=True,
                    )
                )
                if step.prompt_key == "01-scan.md" and not isinstance(
                    output, (ScanCandidatesOutput, NoCandidateOutput)
                ):
                    raise AgentOutputProtocolError(
                        f"Scan phase completed without a <candidates> block. "
                        f"Expected ScanCandidatesOutput or NoCandidateOutput, "
                        f"got {type(output).__name__}."
                    )
                driver.record_outcome(step, output)
                step = driver.next()

            no_candidate = driver.no_candidate
            role_session.discard()

        row.close("finished")
    return ImproveNoCandidate() if no_candidate else ImproveContinue()
