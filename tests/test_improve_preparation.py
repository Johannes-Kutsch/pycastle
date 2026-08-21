from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pycastle.agents.output_protocol import (
    CompletionOutput,
    NoCandidateOutput,
    ScanCandidateItem,
    ScanCandidatesOutput,
)
from pycastle.iteration.improve import ImprovePhaseDriver
from pycastle.iteration.improve_preparation import (
    ImproveCandidate,
    ImproveStepPreparationRequest,
    prepare_improve_step,
)
from pycastle.iteration.improve_role_session_store import (
    CandidateItem,
    CandidateList,
    CandidateRecord,
    ImproveRoleSessionStore,
)
from pycastle.prompts.pipeline import PromptRenderError, PromptTemplate
from pycastle.services import GithubNetworkError


@dataclass
class _GithubPortStandIn:
    recent_prds: list[dict[str, object]] = field(default_factory=list)
    issue: dict[str, object] = field(
        default_factory=lambda: {"number": 42, "title": "PRD", "body": "body"}
    )
    comments: list[dict[str, str]] = field(default_factory=list)
    recent_prd_calls: int = 0
    issue_calls: list[int] = field(default_factory=list)
    issue_comment_calls: list[int] = field(default_factory=list)
    recent_prd_error: Exception | None = None
    issue_error: Exception | None = None

    def get_recent_improve_prds(self) -> list[dict[str, object]]:
        self.recent_prd_calls += 1
        if self.recent_prd_error is not None:
            raise self.recent_prd_error
        return self.recent_prds

    def get_issue(self, issue_number: int) -> dict[str, object]:
        self.issue_calls.append(issue_number)
        if self.issue_error is not None:
            raise self.issue_error
        return self.issue

    def get_issue_comments(self, issue_number: int) -> list[dict[str, str]]:
        self.issue_comment_calls.append(issue_number)
        return self.comments


def test_prepare_improve_step_builds_exact_scan_payload(tmp_path: Path):
    driver = ImprovePhaseDriver(tmp_path / "improve", no_candidate_report=True)
    step = driver.start()
    assert step is not None
    github_port = _GithubPortStandIn(
        recent_prds=[{"number": 12, "state": "OPEN", "title": "First candidate"}]
    )

    prepared = prepare_improve_step(
        step,
        short_sid="abcd1234",
        github_port=github_port,
        candidate_budget=3,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_SCAN
    assert prepared.session_namespace == "main"
    assert prepared.name == "Scan Agent"
    assert prepared.work_body == "picking an improvement"
    assert prepared.prompt.send_role_prompt_on_resume is False
    assert prepared.prompt.scope_args == {
        "RECENT_IMPROVE_PRD_TITLES": "#12 OPEN - First candidate",
        "CANDIDATE_BUDGET": "3",
    }
    assert github_port.recent_prd_calls == 1


def test_prepare_improve_step_builds_exact_prd_payload_from_driver_step(
    tmp_path: Path,
):
    driver = ImprovePhaseDriver(tmp_path / "improve-prd", no_candidate_report=True)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(
        step1,
        ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="Refactor"),)),
    )
    step2 = driver.next()
    assert step2 is not None
    github_port = _GithubPortStandIn(
        recent_prds=[
            {"number": 12, "state": "OPEN", "title": "First candidate"},
            {"number": 11, "state": "CLOSED", "title": "Second candidate"},
        ]
    )

    prepared = prepare_improve_step(
        step2,
        short_sid="abcd1234",
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_PRD
    assert prepared.session_namespace == "candidate/0"
    assert prepared.name == "PRD Agent"
    assert prepared.work_body == "writing PRD"
    assert prepared.prompt.send_role_prompt_on_resume is True
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_PRDS": (
            "#12 OPEN - First candidate\n#11 CLOSED - Second candidate"
        ),
        "CANDIDATE_RANK": "1",
        "CANDIDATE_TITLE": "Refactor",
    }
    assert github_port.recent_prd_calls == 1
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_builds_exact_no_candidate_report_payload_from_driver_step(
    tmp_path: Path,
):
    driver = ImprovePhaseDriver(
        tmp_path / "improve-no-candidate", no_candidate_report=True
    )
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(step1, NoCandidateOutput())
    step2 = driver.next()
    assert step2 is not None
    github_port = _GithubPortStandIn(
        recent_prds=[
            {"number": 12, "state": "OPEN", "title": "First candidate"},
            {"number": 11, "state": "CLOSED", "title": "Second candidate"},
        ]
    )

    prepared = prepare_improve_step(
        step2,
        short_sid="abcd1234",
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_NO_CANDIDATE
    assert prepared.session_namespace == "main"
    assert prepared.name == "Rejection Report Agent"
    assert prepared.work_body == "filing no-candidate report"
    assert prepared.prompt.send_role_prompt_on_resume is True
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_PRDS": (
            "#12 OPEN - First candidate\n#11 CLOSED - Second candidate"
        ),
        "CANDIDATE_RANK": "",
        "CANDIDATE_TITLE": "",
    }
    assert github_port.recent_prd_calls == 1
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_builds_exact_prd_payload_without_lookup_policy_flag():
    github_port = _GithubPortStandIn(
        recent_prds=[
            {"number": 12, "state": "OPEN", "title": "First candidate"},
            {"number": 11, "state": "CLOSED", "title": "Second candidate"},
        ]
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_PRD,
            session_namespace="main",
            display_name="PRD Agent",
            work_body="writing PRD",
            send_role_prompt_on_resume=True,
            short_sid="abcd1234",
            candidate=ImproveCandidate(rank=1, title="Refactor"),
        ),
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_PRD
    assert prepared.session_namespace == "main"
    assert prepared.name == "PRD Agent"
    assert prepared.work_body == "writing PRD"
    assert prepared.prompt.send_role_prompt_on_resume is True
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_PRDS": (
            "#12 OPEN - First candidate\n#11 CLOSED - Second candidate"
        ),
        "CANDIDATE_RANK": "1",
        "CANDIDATE_TITLE": "Refactor",
    }
    assert github_port.recent_prd_calls == 1


def test_prepare_improve_step_uses_exact_empty_recent_prd_message_for_prd_template():
    github_port = _GithubPortStandIn(recent_prds=[])

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_PRD,
            session_namespace="main",
            display_name="PRD Agent",
            work_body="body",
            send_role_prompt_on_resume=True,
            short_sid="abcd1234",
            candidate=ImproveCandidate(rank=2, title="Deepen module"),
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_PRDS": "No recent improve PRDs found.",
        "CANDIDATE_RANK": "2",
        "CANDIDATE_TITLE": "Deepen module",
    }
    assert github_port.recent_prd_calls == 1


def test_prepare_improve_step_uses_exact_empty_recent_prd_message_for_no_candidate_template():
    github_port = _GithubPortStandIn(recent_prds=[])

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_NO_CANDIDATE,
            session_namespace="main",
            display_name="Rejection Report Agent",
            work_body="body",
            send_role_prompt_on_resume=True,
            short_sid="abcd1234",
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_PRDS": "No recent improve PRDs found.",
        "CANDIDATE_RANK": "",
        "CANDIDATE_TITLE": "",
    }
    assert github_port.recent_prd_calls == 1


def test_prepare_improve_step_uses_short_sid_only_for_issues():
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_ISSUES,
            session_namespace="main",
            display_name="Slice Agent",
            work_body="filing sub-issues",
            send_role_prompt_on_resume=True,
            short_sid="abcd1234",
            fetch_recent_prd_titles=False,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.recent_prd_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_resumed_scan_uses_empty_recent_prd_message(
    tmp_path: Path,
):
    driver_dir = tmp_path / "improve"
    driver_dir.mkdir(parents=True, exist_ok=True)
    ImproveRoleSessionStore(driver_dir).write_in_flight("01-scan")
    driver = ImprovePhaseDriver(driver_dir, no_candidate_report=True)
    step = driver.start()

    assert step is not None
    github_port = _GithubPortStandIn(
        recent_prd_error=AssertionError("mid-phase scan retries must not refetch PRDs")
    )

    prepared = prepare_improve_step(
        step,
        short_sid="abcd1234",
        github_port=github_port,
        candidate_budget=2,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_SCAN
    assert prepared.session_namespace == "main"
    assert prepared.name == "Scan Agent"
    assert prepared.work_body == "picking an improvement"
    assert prepared.prompt.send_role_prompt_on_resume is False
    assert prepared.prompt.scope_args == {
        "RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found.",
        "CANDIDATE_BUDGET": "2",
    }
    assert github_port.recent_prd_calls == 0


def test_prepare_improve_step_issues_scope_contains_only_short_sid():
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_ISSUES,
            session_namespace="main",
            display_name="Slice Agent",
            work_body="filing sub-issues",
            send_role_prompt_on_resume=True,
            short_sid="abcd1234",
            fetch_recent_prd_titles=False,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.recent_prd_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_builds_issues_payload_from_driver_step_prd_handoff(
    tmp_path: Path,
):
    driver = ImprovePhaseDriver(tmp_path / "improve-issues", no_candidate_report=True)
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(
        step1,
        ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="Refactor"),)),
    )

    step2 = driver.next()
    assert step2 is not None
    assert step2.prompt_key == "02-prd.md"
    driver.record_outcome(step2, CompletionOutput())

    step3 = driver.next()
    assert step3 is not None
    assert step3.prompt_key == "03-issues.md"
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        step3,
        short_sid="abcd1234",
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_ISSUES
    assert prepared.session_namespace == "candidate/0"
    assert prepared.name == "Slice Agent"
    assert prepared.work_body == "filing sub-issues"
    assert prepared.prompt.send_role_prompt_on_resume is True
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.recent_prd_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_keeps_phase_03_resume_empty_without_parent_prd_handoff(
    tmp_path: Path,
):
    driver_dir = tmp_path / "improve-issues-resume"
    driver_dir.mkdir(parents=True, exist_ok=True)
    # Seed candidate list + a candidate record (PRD done) + in-flight=03-issues
    store = ImproveRoleSessionStore(driver_dir)
    store.write_candidate_list(
        CandidateList(
            candidates=(CandidateItem(rank=1, title="Seeded"),),
            no_candidate=False,
        )
    )
    store.write_cursor(0)
    store.write_candidate_record(
        0,
        CandidateRecord(
            spec_number=None,
            spec_database_id=None,
            spec_title="",
            filed_slices=(),
            labels_applied=False,
        ),
    )
    store.write_in_flight("03-issues")
    driver = ImprovePhaseDriver(driver_dir, no_candidate_report=True)
    step = driver.start()

    assert step is not None
    assert step.prompt_key == "03-issues.md"
    github_port = _GithubPortStandIn(
        issue_error=AssertionError("phase 03 resume without parent PRD must not read")
    )

    prepared = prepare_improve_step(
        step,
        short_sid="abcd1234",
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_ISSUES
    assert prepared.session_namespace == "candidate/0"
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.recent_prd_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_builds_phase_03_payload_during_live_prd_handoff(
    tmp_path: Path,
):
    driver = ImprovePhaseDriver(
        tmp_path / "improve-live-issues", no_candidate_report=True
    )
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(
        step1,
        ScanCandidatesOutput(candidates=(ScanCandidateItem(rank=1, title="Refactor"),)),
    )

    step2 = driver.next()
    assert step2 is not None
    assert step2.prompt_key == "02-prd.md"
    driver.record_outcome(step2, CompletionOutput())

    step3 = driver.next()
    assert step3 is not None
    assert step3.prompt_key == "03-issues.md"
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        step3,
        short_sid="abcd1234",
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_ISSUES
    assert prepared.session_namespace == "candidate/0"
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_propagates_recent_improve_prd_lookup_failures(
    tmp_path: Path,
):
    error = GithubNetworkError("transport error", cause=RuntimeError("boom"))
    driver = ImprovePhaseDriver(tmp_path / "improve-error", no_candidate_report=True)
    step = driver.start()
    assert step is not None
    github_port = _GithubPortStandIn(recent_prd_error=error)

    with pytest.raises(GithubNetworkError) as exc_info:
        prepare_improve_step(
            step,
            short_sid="abcd1234",
            github_port=github_port,
        )

    assert exc_info.value is error


def test_prepare_improve_step_prd_step_candidate_is_set_on_step(tmp_path: Path) -> None:
    """The PRD step returned by the driver carries the candidate from the scan."""
    driver = ImprovePhaseDriver(
        tmp_path / "improve-candidate", no_candidate_report=True
    )
    step1 = driver.start()
    assert step1 is not None
    driver.record_outcome(
        step1,
        ScanCandidatesOutput(
            candidates=(ScanCandidateItem(rank=4, title="My Feature"),)
        ),
    )
    step2 = driver.next()
    assert step2 is not None
    assert step2.prompt_key == "02-prd.md"
    assert step2.candidate == ImproveCandidate(
        rank=4, title="My Feature", spec_number=None
    )


def test_prepare_improve_step_accepts_request_with_candidate(tmp_path: Path) -> None:
    """ImproveStepPreparationRequest with a candidate passes through prepare_improve_step unchanged."""
    candidate = ImproveCandidate(rank=1, title="Foo", spec_number=42)
    request = ImproveStepPreparationRequest(
        prompt_template=PromptTemplate.IMPROVE_ISSUES,
        session_namespace="candidate/0",
        display_name="Slice Agent",
        work_body="filing sub-issues",
        send_role_prompt_on_resume=True,
        short_sid="abcd1234",
        fetch_recent_prd_titles=False,
        candidate=candidate,
    )
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(request, github_port=github_port)

    assert prepared.prompt.template == PromptTemplate.IMPROVE_ISSUES
    assert prepared.prompt.scope_args == {"IMPROVE_SHORT_SID": "abcd1234"}


def test_prepare_improve_step_prd_without_candidate_fails_loudly():
    github_port = _GithubPortStandIn(recent_prds=[])

    with pytest.raises(PromptRenderError):
        prepare_improve_step(
            ImproveStepPreparationRequest(
                prompt_template=PromptTemplate.IMPROVE_PRD,
                session_namespace="main",
                display_name="PRD Agent",
                work_body="writing PRD",
                send_role_prompt_on_resume=True,
                short_sid="abcd1234",
                # candidate intentionally omitted
            ),
            github_port=github_port,
        )


def test_prepare_improve_step_scan_without_candidate_budget_fails_to_render(
    tmp_path: Path,
):
    driver = ImprovePhaseDriver(tmp_path / "improve", no_candidate_report=True)
    step = driver.start()
    assert step is not None
    github_port = _GithubPortStandIn()

    with pytest.raises(PromptRenderError):
        prepare_improve_step(
            step,
            short_sid="abcd1234",
            github_port=github_port,
        )
