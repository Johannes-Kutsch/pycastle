import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from pycastle.agents.output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
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
from pycastle.iteration._rows import StatusRowConfig, status_row
from pycastle.iteration.improve_candidate_lifecycle import (
    Stop,
    file_and_decide,
    reconcile_and_wind_down,
)
from pycastle.iteration.improve_filing import GithubFilingPort
from pycastle.iteration.improve_preparation import (
    ImproveCandidate,
    prepare_improve_step,
)
from pycastle.iteration.improve_role_session_store import (
    CandidateItem,
    CandidateList,
    ImproveRoleSessionStore,
)
from pycastle.iteration.preflight import (
    PreflightAFK,
    PreflightCache,
    PreflightHITL,
)
from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from pycastle.prompts.dispatch import PromptKind
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

_CANDIDATE_NS_PREFIX = "candidate"


def _candidate_namespace(idx: int) -> str:
    return f"{_CANDIDATE_NS_PREFIX}/{idx}"


def _fork_candidate_namespaces(
    sandbox_path: Path,
    candidates: list[ScanCandidateItem],
) -> None:
    """Fork the scan (main) namespace to per-candidate namespaces.

    Idempotent: skips any fork whose target already exists. Skips entirely
    when the main namespace directory does not exist (e.g. in unit tests where
    FakeAgentRunner does not write real session state).
    """
    main_session = RoleSession(sandbox_path, AgentRole.IMPROVE, "main")
    if not main_session.path.is_dir():
        return
    for idx in range(len(candidates)):
        ns = _candidate_namespace(idx)
        target = RoleSession(sandbox_path, AgentRole.IMPROVE, ns)
        if not target.path.is_dir():
            main_session.fork_namespace(ns)


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
    "02-spec.md": _PhaseConfig(
        template=PromptTemplate.IMPROVE_SPEC,
        namespace="main",
        display_name="Spec Agent",
        display_body="writing spec",
    ),
    "03-tickets.md": _PhaseConfig(
        template=PromptTemplate.IMPROVE_TICKETS,
        namespace="main",
        display_name="Tickets Agent",
        display_body="filing tickets",
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
    kind: PromptKind
    fetch_recent_spec_titles: bool
    candidate: ImproveCandidate | None = None
    scan_set_size: int | None = None
    candidate_ordinal: int | None = None


class ImprovePhaseDriver:
    """State machine for the improve pipeline phases.

    Construction is side-effect-free; start() performs the first disk read.

    State is held in three role-level files (via ImproveRoleSessionStore):
    - _candidate_list: JSON with ordered scan candidates (and no_candidate flag).
    - _candidate_cursor: integer index of the candidate currently being processed.
    - _in_flight: key of the phase currently executing (for mid-phase resumption).

    Per-candidate state lives in candidates/<idx>/_candidate_record (written by the
    filing pass and the spec phase completion marker).
    """

    def __init__(self, role_session_dir: Path, *, no_candidate_report: bool) -> None:
        self._store = ImproveRoleSessionStore(role_session_dir)
        self._no_candidate_report = no_candidate_report
        self._candidates: list[ScanCandidateItem] | None = None
        self._no_candidate: bool = False
        self._cursor: int = 0

    # ── Step factories ────────────────────────────────────────────────────────

    def _make_scan_step(self, *, fetch_recent_spec_titles: bool) -> Step:
        return Step(
            prompt_key="01-scan.md",
            cfg=_PHASES["01-scan.md"],
            kind=PromptKind.ROLE_PROMPT,
            fetch_recent_spec_titles=fetch_recent_spec_titles,
        )

    def _make_spec_step(
        self,
        *,
        kind: PromptKind,
        idx: int,
        candidate: ImproveCandidate,
    ) -> Step:
        cfg = dataclasses.replace(
            _PHASES["02-spec.md"], namespace=_candidate_namespace(idx)
        )
        candidates = self._candidates
        return Step(
            prompt_key="02-spec.md",
            cfg=cfg,
            kind=kind,
            fetch_recent_spec_titles=True,
            candidate=candidate,
            scan_set_size=len(candidates) if candidates is not None else None,
            candidate_ordinal=idx + 1,
        )

    def _make_issues_step(self, *, idx: int, candidate: ImproveCandidate) -> Step:
        cfg = dataclasses.replace(
            _PHASES["03-tickets.md"], namespace=_candidate_namespace(idx)
        )
        candidates = self._candidates
        return Step(
            prompt_key="03-tickets.md",
            cfg=cfg,
            kind=PromptKind.FOLLOW_UP,
            fetch_recent_spec_titles=False,
            candidate=candidate,
            scan_set_size=len(candidates) if candidates is not None else None,
            candidate_ordinal=idx + 1,
        )

    def _make_report_step(self, *, kind: PromptKind) -> Step:
        return Step(
            prompt_key="04-no-candidate-report.md",
            cfg=_PHASES["04-no-candidate-report.md"],
            kind=kind,
            fetch_recent_spec_titles=True,
        )

    # ── Core state resolution ─────────────────────────────────────────────────

    def _step_for_candidate(self, idx: int, *, from_start: bool) -> Step | None:
        """Return the next step for candidate at idx, or None if already complete."""
        record = self._store.read_candidate_record(idx)

        if record is not None and record.labels_applied:
            return None  # Candidate fully complete

        candidates = self._candidates
        if candidates is None:
            return None
        c = candidates[idx]
        spec_number = record.spec_number if record is not None else None
        candidate = ImproveCandidate(
            rank=c.rank, title=c.title, spec_number=spec_number
        )

        if record is None:
            # No record → spec phase. Check in-flight for mid-spec resume.
            in_flight = self._store.read_in_flight() if from_start else None
            is_mid_spec = in_flight == "02-spec"
            return self._make_spec_step(
                kind=PromptKind.ROLE_PROMPT if is_mid_spec else PromptKind.FOLLOW_UP,
                idx=idx,
                candidate=candidate,
            )

        # Record exists → slice (Issues) phase.
        in_flight = self._store.read_in_flight() if from_start else None
        is_mid_issues = in_flight == "03-tickets"
        step = self._make_issues_step(idx=idx, candidate=candidate)
        # For mid-issues resume, override kind to ROLE_PROMPT.
        if is_mid_issues:
            step = Step(
                prompt_key=step.prompt_key,
                cfg=step.cfg,
                kind=PromptKind.ROLE_PROMPT,
                fetch_recent_spec_titles=step.fetch_recent_spec_titles,
                candidate=step.candidate,
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
            self._store.write_cursor(cursor)
        return None  # All candidates done

    # ── Public interface ──────────────────────────────────────────────────────

    def start(self) -> "Step | None":
        candidate_list = self._store.read_candidate_list()

        if candidate_list is None:
            # Scan not done → return scan step.
            in_flight = self._store.read_in_flight()
            is_mid_scan = in_flight == "01-scan"
            step = self._make_scan_step(fetch_recent_spec_titles=not is_mid_scan)
            self._store.write_in_flight("01-scan")
            return step

        candidates = [
            ScanCandidateItem(rank=c.rank, title=c.title)
            for c in candidate_list.candidates
        ]
        no_candidate = candidate_list.no_candidate
        cursor = self._store.read_cursor() or 0

        self._candidates = candidates
        self._no_candidate = no_candidate
        self._cursor = cursor

        if no_candidate:
            if self._no_candidate_report and cursor == 0:
                in_flight = self._store.read_in_flight()
                is_mid_report = in_flight == "04-no-candidate-report"
                step = self._make_report_step(
                    kind=PromptKind.ROLE_PROMPT
                    if is_mid_report
                    else PromptKind.FOLLOW_UP
                )
                self._store.write_in_flight("04-no-candidate-report")
                return step
            return None

        next_step: Step | None = self._next_step_from_cursor(cursor, from_start=True)
        if next_step is not None:
            self._store.write_in_flight(next_step.prompt_key.removesuffix(".md"))
        return next_step

    def next(self) -> "Step | None":
        candidates = self._candidates
        if candidates is None:
            return None

        if self._no_candidate:
            if self._no_candidate_report and self._cursor == 0:
                step = self._make_report_step(kind=PromptKind.FOLLOW_UP)
                self._store.write_in_flight("04-no-candidate-report")
                return step
            return None

        next_step: Step | None = self._next_step_from_cursor(
            self._cursor, from_start=False
        )
        if next_step is not None:
            self._store.write_in_flight(next_step.prompt_key.removesuffix(".md"))
        return next_step

    def record_outcome(self, step: "Step", output: AgentOutput) -> None:
        if step.prompt_key == "01-scan.md":
            if isinstance(output, ScanCandidatesOutput):
                candidates = list(output.candidates)
                self._candidates = candidates
                self._no_candidate = False
                self._cursor = 0
                self._store.write_candidate_list(
                    CandidateList(
                        candidates=tuple(
                            CandidateItem(rank=c.rank, title=c.title)
                            for c in candidates
                        ),
                        no_candidate=False,
                    )
                )
                self._store.write_cursor(0)
            elif isinstance(output, NoCandidateOutput):
                self._candidates = []
                self._no_candidate = True
                self._cursor = 0
                self._store.write_candidate_list(
                    CandidateList(candidates=(), no_candidate=True)
                )
                self._store.write_cursor(0)

        elif step.prompt_key == "02-spec.md":
            self._store.mark_spec_completion(self._cursor)

        elif step.prompt_key == "03-tickets.md":
            self._cursor += 1
            self._store.write_cursor(self._cursor)

        elif step.prompt_key == "04-no-candidate-report.md":
            # cursor=1 marks report done; checked in start() and next().
            self._cursor = 1
            self._store.write_cursor(1)

        self._store.clear_in_flight()

    @property
    def no_candidate(self) -> bool:
        return self._no_candidate

    @property
    def candidate_count(self) -> int | None:
        return None if self._candidates is None else len(self._candidates)


@dataclass(frozen=True)
class ImproveNoCandidate:
    pass


@dataclass(frozen=True)
class ImproveContinue:
    completed_count: int = 0

    @property
    def completed(self) -> bool:
        return self.completed_count > 0


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


def _needs_candidate_gate(step: "Step | None") -> bool:
    return (
        step is not None
        and step.prompt_key == "02-spec.md"
        and step.kind is PromptKind.FOLLOW_UP
    )


def _candidate_transcript_ok(
    step: "Step",
    deps: "_ImproveDeps",
    sandbox_path: Path,
) -> bool:
    service_registry = deps.service_registry
    service = (
        service_registry[deps.cfg.improve_override.service]
        if service_registry is not None
        else None
    )
    if service is None:
        return False
    return has_exact_transcript_match(
        worktree=sandbox_path,
        role=AgentRole.IMPROVE,
        session_namespace=step.cfg.namespace,
        service=cast("AgentService", service),
    )


def _improve_step_body(
    step: "Step",
    *,
    n_candidates: int,
    improve_dispatched_count: int,
    improve_max: int | None,
) -> str | None:
    """Return the Improve phase body for the given step, or None for no change."""
    if step.prompt_key in ("02-spec.md", "03-tickets.md"):
        candidate_idx = int(step.cfg.namespace.split("/")[1])
        k = candidate_idx + 1
        if improve_max is not None:
            return f"candidate {k}/{n_candidates} · improvement {improve_dispatched_count + k}/{improve_max}"
        return f"candidate {k}/{n_candidates}"
    if step.prompt_key == "04-no-candidate-report.md":
        return "filing no-candidate report"
    return None


def _announce_candidate(
    step: "Step",
    *,
    status_display: "StatusDisplay",
    candidate_count: int,
    last_announced_idx: int,
) -> int:
    """Print the candidate start line if this is a new candidate. Returns updated last_announced_idx."""
    if step.prompt_key not in ("02-spec.md", "03-tickets.md"):
        return last_announced_idx
    candidate_idx = int(step.cfg.namespace.split("/")[1])
    if candidate_idx == last_announced_idx:
        return last_announced_idx
    title = step.candidate.title if step.candidate else ""
    status_display.print(
        "Improve",
        f'→ candidate {candidate_idx + 1}/{candidate_count} "{title}"',
    )
    return candidate_idx


def _verify_scan_output(step: "Step", output: "AgentOutput") -> None:
    """Raise AgentOutputProtocolError if a scan step returned an unexpected output type."""
    if step.prompt_key == "01-scan.md" and not isinstance(
        output, (ScanCandidatesOutput, NoCandidateOutput)
    ):
        raise AgentOutputProtocolError(
            f"Scan phase completed without a <candidates> block. "
            f"Expected ScanCandidatesOutput or NoCandidateOutput, "
            f"got {type(output).__name__}."
        )


async def improve_phase(
    deps: _ImproveDeps,
) -> ImproveNoCandidate | ImproveContinue | PreflightHITL | PreflightAFK:
    """Run the improve pipeline."""
    async with status_row(
        deps.status_display,
        "Improve",
        kind="phase",
        must_close=True,
        config=StatusRowConfig(initial_phase="Running"),
    ) as row:
        deps.status_display.update_phase("Improve", "scanning for candidates")
        verdict = await deps.preflight_cache.get_safe_sha(deps)
        if isinstance(verdict, (PreflightHITL, PreflightAFK)):
            row.close(f"preflight gate blocked (issue #{verdict.issue_number})")
            return verdict

        fingerprint = verdict.sha
        pre_sandbox_path = reusable_sandbox_worktree_identity(
            IMPROVE_SANDBOX_INTENT, deps.repo_root
        ).path
        reconcile_and_wind_down(
            pre_sandbox_path,
            fingerprint=fingerprint,
            port=GithubFilingPort(deps.github_svc),
            cfg=deps.cfg,
        )

        async with reusable_sandbox_worktree(
            IMPROVE_SANDBOX_INTENT,
            sha=verdict.sha,
            deps=deps,
            operating_branch=deps.cfg.operating_branch,
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
            if _needs_candidate_gate(step) and not _candidate_transcript_ok(
                cast("Step", step), deps, sandbox_path
            ):
                deps.status_display.print(
                    "Improve",
                    "Restarting improve from phase 1 because the phase 1 transcript handoff is unavailable for a clean phase 2 entry.",
                )
                role_session.discard()
                row.close("restarting from phase 1")
                return ImproveContinue(completed_count=0)

            role_session.write_fingerprint(fingerprint)

            candidate_budget = compute_candidate_budget(
                candidates_per_scan=deps.cfg.improve_candidates_per_scan,
                improve_max=deps.cfg.improve_max,
                dispatched=deps.improve_dispatched_count,
            )
            completed_count = 0
            last_announced_idx = -1
            while step is not None:
                n_candidates = driver.candidate_count or 0
                last_announced_idx = _announce_candidate(
                    step,
                    status_display=deps.status_display,
                    candidate_count=n_candidates,
                    last_announced_idx=last_announced_idx,
                )
                body = _improve_step_body(
                    step,
                    n_candidates=n_candidates,
                    improve_dispatched_count=deps.improve_dispatched_count,
                    improve_max=deps.cfg.improve_max,
                )
                if body is not None:
                    deps.status_display.update_phase("Improve", body)

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
                # Save namespace before record_outcome advances the cursor.
                step_namespace = prepared_step.session_namespace
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
                        session_namespace=step_namespace,
                        preserve_session_on_completion=True,
                    )
                )
                _verify_scan_output(step, output)
                driver.record_outcome(step, output)

                # After scan, eagerly fork one namespace per candidate.
                if step.prompt_key == "01-scan.md" and isinstance(
                    output, ScanCandidatesOutput
                ):
                    _fork_candidate_namespaces(sandbox_path, list(output.candidates))

                if step.prompt_key == "03-tickets.md":
                    outcome = await file_and_decide(
                        step_namespace=step_namespace,
                        deps=deps,
                        role_session_dir=role_session_dir,
                        sandbox_path=sandbox_path,
                        fingerprint=fingerprint,
                        completed_count=completed_count,
                    )
                    completed_count = outcome.completed_count
                    if isinstance(outcome, Stop) and outcome.reason in (
                        "cap-reached",
                        "safe-sha-changed",
                    ):
                        break

                step = driver.next()

            no_candidate = driver.no_candidate

            role_session.discard()

        if no_candidate:
            row.close("no candidate")
        else:
            row.close(f"filed {completed_count} improvement(s)")
    return (
        ImproveNoCandidate()
        if no_candidate
        else ImproveContinue(completed_count=completed_count)
    )
