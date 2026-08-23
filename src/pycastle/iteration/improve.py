import contextlib
import dataclasses
import shutil
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
from pycastle.bug_reporter import file_unrepairable_draft_set_issue
from pycastle.config import Config
from pycastle.display.status_display import StatusDisplay
from pycastle.errors import SetupPhaseError
from pycastle.infrastructure.worktree import (
    SandboxWorktreeIntent,
    reusable_sandbox_worktree,
    reusable_sandbox_worktree_identity,
)
from pycastle.iteration._rows import StatusRowConfig, status_row
from pycastle.iteration.improve_drafts import DraftSetValidationError, read_draft_set
from pycastle.iteration.improve_filing import file_draft_set
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
    PreflightReady,
)
from pycastle.managed_worktree_mount_policy import (
    ManagedWorktreeMountRejected,
    decide_managed_worktree_mount,
    describe_managed_worktree_mount_rejection,
    should_reject_managed_worktree_mount,
)
from pycastle.prompts.dispatch import PromptKind, build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.prompts.scope_args import (
    compute_candidate_budget,
    validated_scope_args_for_template,
)
from pycastle.runtime_session import session_uuid
from pycastle.services import GitService, ServiceRegistry
from pycastle.services.github_service import GithubService
from pycastle.session import RoleSession, has_exact_transcript_match

if TYPE_CHECKING:
    from pycastle.services.runtime_services import AgentService

IMPROVE_SANDBOX_INTENT = SandboxWorktreeIntent.IMPROVE
IMPROVE_SANDBOX = f"pycastle/{IMPROVE_SANDBOX_INTENT.value}"

_DRAFTS_SUBDIR = "_drafts"
_CANDIDATE_NS_PREFIX = "candidate"
_MAX_CORRECTION_ATTEMPTS = 3


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


class _GithubFilingPort:
    def __init__(self, svc: GithubService) -> None:
        self._svc = svc

    def create_issue(self, title: str, body: str, labels: list[str]) -> tuple[int, int]:
        return self._svc.create_issue_in(self._svc.repo, title, body, labels)

    def register_sub_issue(self, parent_number: int, child_database_id: int) -> None:
        self._svc.add_sub_issue(parent_number, child_database_id)

    def add_issue_dependency(self, child_number: int, blocker_database_id: int) -> None:
        self._svc.add_issue_dependency(child_number, blocker_database_id)

    def apply_label(self, issue_number: int, label: str) -> None:
        self._svc.add_label_to_issue(issue_number, label)

    def close_issue(self, issue_number: int) -> None:
        self._svc.close_issue(issue_number)


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
    kind: PromptKind
    fetch_recent_prd_titles: bool
    candidate: ImproveCandidate | None = None


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

    def _make_scan_step(self, *, fetch_recent_prd_titles: bool) -> Step:
        return Step(
            prompt_key="01-scan.md",
            cfg=_PHASES["01-scan.md"],
            kind=PromptKind.ROLE_PROMPT,
            fetch_recent_prd_titles=fetch_recent_prd_titles,
        )

    def _make_prd_step(
        self,
        *,
        kind: PromptKind,
        idx: int,
        candidate: ImproveCandidate,
    ) -> Step:
        cfg = dataclasses.replace(
            _PHASES["02-prd.md"], namespace=_candidate_namespace(idx)
        )
        return Step(
            prompt_key="02-prd.md",
            cfg=cfg,
            kind=kind,
            fetch_recent_prd_titles=True,
            candidate=candidate,
        )

    def _make_issues_step(self, *, idx: int, candidate: ImproveCandidate) -> Step:
        cfg = dataclasses.replace(
            _PHASES["03-issues.md"], namespace=_candidate_namespace(idx)
        )
        return Step(
            prompt_key="03-issues.md",
            cfg=cfg,
            kind=PromptKind.FOLLOW_UP,
            fetch_recent_prd_titles=False,
            candidate=candidate,
        )

    def _make_report_step(self, *, kind: PromptKind) -> Step:
        return Step(
            prompt_key="04-no-candidate-report.md",
            cfg=_PHASES["04-no-candidate-report.md"],
            kind=kind,
            fetch_recent_prd_titles=True,
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
            # No record → spec (PRD) phase. Check in-flight for mid-PRD resume.
            in_flight = self._store.read_in_flight() if from_start else None
            is_mid_prd = in_flight == "02-prd"
            return self._make_prd_step(
                kind=PromptKind.ROLE_PROMPT if is_mid_prd else PromptKind.FOLLOW_UP,
                idx=idx,
                candidate=candidate,
            )

        # Record exists → slice (Issues) phase.
        in_flight = self._store.read_in_flight() if from_start else None
        is_mid_issues = in_flight == "03-issues"
        step = self._make_issues_step(idx=idx, candidate=candidate)
        # For mid-issues resume, override kind to ROLE_PROMPT.
        if is_mid_issues:
            step = Step(
                prompt_key=step.prompt_key,
                cfg=step.cfg,
                kind=PromptKind.ROLE_PROMPT,
                fetch_recent_prd_titles=step.fetch_recent_prd_titles,
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
            step = self._make_scan_step(fetch_recent_prd_titles=not is_mid_scan)
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

        elif step.prompt_key == "02-prd.md":
            self._store.mark_prd_completion(self._cursor)

        elif step.prompt_key == "03-issues.md":
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
        and step.prompt_key == "02-prd.md"
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


def _cap_reached(deps: "_ImproveDeps", completed_count: int) -> bool:
    return (
        deps.cfg.improve_max is not None
        and deps.improve_dispatched_count + completed_count >= deps.cfg.improve_max
    )


def _prev_spec(
    store: ImproveRoleSessionStore, candidate_idx: int
) -> tuple[int, int] | None:
    """Return (spec_number, spec_database_id) from the previous candidate record, if available."""
    if candidate_idx == 0:
        return None
    prev_record = store.read_candidate_record(candidate_idx - 1)
    if (
        prev_record is not None
        and prev_record.spec_number is not None
        and prev_record.spec_database_id is not None
    ):
        return (prev_record.spec_number, prev_record.spec_database_id)
    return None


async def _file_improve_drafts(
    *,
    deps: _ImproveDeps,
    role_session_dir: Path,
    sandbox_path: Path,
    candidate_idx: int,
    candidate_namespace: str,
) -> bool:
    """Attempt to read, correct, and file the improve draft set.

    Returns True when the draft set was filed successfully, False when all
    correction attempts were exhausted and the candidate was abandoned (an issue
    is filed on the consuming project's tracker and the drafts dir is cleared).
    """
    draft_dir = role_session_dir / _DRAFTS_SUBDIR
    store = ImproveRoleSessionStore(role_session_dir)
    prev_spec = _prev_spec(store, candidate_idx)

    last_exc: DraftSetValidationError | None = None
    drafts = None
    for attempt in range(_MAX_CORRECTION_ATTEMPTS + 1):
        try:
            drafts = read_draft_set(draft_dir, deps.cfg)
            last_exc = None
            break
        except DraftSetValidationError as exc:
            last_exc = exc
            if attempt < _MAX_CORRECTION_ATTEMPTS:
                validation_errors = "\n".join(exc.problems)
                correction_prompt = build_prompt_invocation(
                    PromptTemplate.IMPROVE_DRAFT_CORRECTION,
                    validated_scope_args_for_template(
                        PromptTemplate.IMPROVE_DRAFT_CORRECTION,
                        {"VALIDATION_ERRORS": validation_errors},
                    ),
                    kind=PromptKind.FOLLOW_UP,
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
                        work_body="fixing draft validation errors",
                        session_namespace=candidate_namespace,
                        preserve_session_on_completion=True,
                    )
                )

    if last_exc is not None:
        draft_file_contents: dict[str, str] = {}
        if draft_dir.is_dir():
            for f in sorted(draft_dir.glob("*.md")):
                with contextlib.suppress(OSError):
                    draft_file_contents[f.name] = f.read_text(encoding="utf-8")
        file_unrepairable_draft_set_issue(
            problems=last_exc.problems,
            draft_files=draft_file_contents,
            github_svc=deps.github_svc,
        )
        if draft_dir.is_dir():
            shutil.rmtree(draft_dir)
        return False

    if drafts is None:
        return False  # unreachable; loop always sets drafts on break
    file_draft_set(
        drafts,
        port=_GithubFilingPort(deps.github_svc),
        store=store,
        candidate_idx=candidate_idx,
        state_label=deps.cfg.issue_label,
        prev_spec=prev_spec,
    )
    # Clear draft dir so the next candidate starts with a clean slate.
    if draft_dir.is_dir():
        shutil.rmtree(draft_dir)
    return True


def _wind_down_partial_candidates(
    role_session_dir: Path,
    *,
    port: "_GithubFilingPort",
    cfg: "Config",
) -> None:
    """Handle partially-filed candidates when the safe SHA changes (AC2, AC3).

    Reads the candidate list and cursor from the store.  For each candidate at
    or after the cursor that has not yet been fully labelled:
    - spec filed but no slices → close the spec (AC2)
    - some slices filed but not labelled → complete filing by host (AC3)
    """
    store = ImproveRoleSessionStore(role_session_dir)
    candidate_list = store.read_candidate_list()
    if candidate_list is None:
        return
    candidate_count = len(candidate_list.candidates)
    cursor = store.read_cursor() or 0

    for idx in range(cursor, candidate_count):
        record = store.read_candidate_record(idx)
        if record is None or record.spec_number is None or record.labels_applied:
            continue
        if not record.filed_slices:
            port.close_issue(record.spec_number)
        else:
            draft_dir = role_session_dir / _DRAFTS_SUBDIR
            try:
                drafts = read_draft_set(draft_dir, cfg)
                file_draft_set(
                    drafts,
                    port=port,
                    store=store,
                    candidate_idx=idx,
                    state_label=cfg.issue_label,
                    prev_spec=_prev_spec(store, idx),
                )
            except DraftSetValidationError:
                pass


def _gate_and_wind_down(
    pre_sandbox_path: Path,
    *,
    fingerprint: str,
    port: "_GithubFilingPort",
    cfg: "Config",
) -> None:
    """Discard the improve session if the fingerprint changed.

    Before discarding, wind down any partially-filed candidates (AC2, AC3) so
    that spec-only candidates are closed and partly-sliced ones are labelled.
    """
    pre_role_session = RoleSession(pre_sandbox_path, AgentRole.IMPROVE)
    if pre_role_session.read_fingerprint() != fingerprint:
        _wind_down_partial_candidates(pre_role_session.path, port=port, cfg=cfg)
        pre_role_session.discard()


async def _complete_candidate(
    *,
    step_namespace: str,
    deps: "_ImproveDeps",
    role_session_dir: Path,
    sandbox_path: Path,
    fingerprint: str,
    completed_count: int,
) -> tuple[int, bool]:
    """File drafts and check stop conditions after a 03-issues.md step.

    Returns (new_completed_count, should_stop) where should_stop is True when
    either the cap is reached or the safe SHA changed (AC1).  An abandoned
    candidate (unrepairable draft set) returns (completed_count, False) so the
    loop advances to the next candidate without counting the failed one.
    """
    candidate_idx = int(step_namespace.split("/")[1])
    filed = await _file_improve_drafts(
        deps=deps,
        role_session_dir=role_session_dir,
        sandbox_path=sandbox_path,
        candidate_idx=candidate_idx,
        candidate_namespace=step_namespace,
    )
    if not filed:
        return completed_count, False
    new_count = completed_count + 1
    if _cap_reached(deps, new_count):
        return new_count, True
    mid_verdict = await deps.preflight_cache.get_safe_sha(deps)
    sha_changed = (
        not isinstance(mid_verdict, PreflightReady) or mid_verdict.sha != fingerprint
    )
    return new_count, sha_changed


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
        _gate_and_wind_down(
            pre_sandbox_path,
            fingerprint=fingerprint,
            port=_GithubFilingPort(deps.github_svc),
            cfg=deps.cfg,
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
                if step.prompt_key == "01-scan.md" and not isinstance(
                    output, (ScanCandidatesOutput, NoCandidateOutput)
                ):
                    raise AgentOutputProtocolError(
                        f"Scan phase completed without a <candidates> block. "
                        f"Expected ScanCandidatesOutput or NoCandidateOutput, "
                        f"got {type(output).__name__}."
                    )
                driver.record_outcome(step, output)

                # After scan, eagerly fork one namespace per candidate.
                if step.prompt_key == "01-scan.md" and isinstance(
                    output, ScanCandidatesOutput
                ):
                    _fork_candidate_namespaces(sandbox_path, list(output.candidates))

                if step.prompt_key == "03-issues.md":
                    completed_count, stop = await _complete_candidate(
                        step_namespace=step_namespace,
                        deps=deps,
                        role_session_dir=role_session_dir,
                        sandbox_path=sandbox_path,
                        fingerprint=fingerprint,
                        completed_count=completed_count,
                    )
                    if stop:
                        break

                step = driver.next()

            no_candidate = driver.no_candidate

            role_session.discard()

        row.close("finished")
    return (
        ImproveNoCandidate()
        if no_candidate
        else ImproveContinue(completed_count=completed_count)
    )
