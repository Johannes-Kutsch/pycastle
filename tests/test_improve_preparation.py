from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pycastle.agents.output_protocol import (
    ScanCandidateItem,
    ScanCandidatesOutput,
)
from pycastle.iteration.improve import ImprovePhaseDriver
from pycastle.iteration.improve_preparation import (
    ImproveCandidate,
    ImproveStepPreparationRequest,
    prepare_improve_step,
)
from pycastle.prompts.dispatch import PromptKind
from pycastle.prompts.pipeline import PromptRenderError, PromptTemplate
from pycastle.services import GithubNetworkError


@dataclass
class _GithubPortStandIn:
    recent_specs: list[dict[str, object]] = field(default_factory=list)
    issue: dict[str, object] = field(
        default_factory=lambda: {"number": 42, "title": "spec", "body": "body"}
    )
    comments: list[dict[str, str]] = field(default_factory=list)
    recent_spec_calls: int = 0
    issue_calls: list[int] = field(default_factory=list)
    issue_comment_calls: list[int] = field(default_factory=list)
    recent_spec_error: Exception | None = None
    issue_error: Exception | None = None

    def get_recent_improve_specs(self) -> list[dict[str, object]]:
        self.recent_spec_calls += 1
        if self.recent_spec_error is not None:
            raise self.recent_spec_error
        return self.recent_specs

    def get_issue(self, issue_number: int) -> dict[str, object]:
        self.issue_calls.append(issue_number)
        if self.issue_error is not None:
            raise self.issue_error
        return self.issue

    def get_issue_comments(self, issue_number: int) -> list[dict[str, str]]:
        self.issue_comment_calls.append(issue_number)
        return self.comments


def test_prepare_improve_step_builds_exact_scan_payload():
    github_port = _GithubPortStandIn(
        recent_specs=[{"number": 12, "state": "OPEN", "title": "First candidate"}]
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SCAN,
            session_namespace="main",
            display_name="Scan Agent",
            work_body="picking up to 3 improvements",
            kind=PromptKind.ROLE_PROMPT,
            short_sid="abcd1234",
            fetch_recent_spec_titles=True,
            candidate_budget=3,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_SCAN
    assert prepared.session_namespace == "main"
    assert prepared.name == "Scan Agent"
    assert prepared.work_body == "picking up to 3 improvements"
    assert prepared.prompt.kind is PromptKind.ROLE_PROMPT
    assert prepared.prompt.scope_args == {
        "RECENT_IMPROVE_SPEC_TITLES": "#12 OPEN - First candidate",
        "CANDIDATE_BUDGET": "3",
    }
    assert github_port.recent_spec_calls == 1


def test_prepare_improve_step_builds_exact_spec_payload_from_driver_step():
    github_port = _GithubPortStandIn(
        recent_specs=[
            {"number": 12, "state": "OPEN", "title": "First candidate"},
            {"number": 11, "state": "CLOSED", "title": "Second candidate"},
        ]
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SPEC,
            session_namespace="candidate/0",
            display_name="Spec Agent",
            work_body='writing spec for candidate 1/1 "Refactor"',
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=True,
            candidate=ImproveCandidate(rank=1, title="Refactor"),
        ),
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_SPEC
    assert prepared.session_namespace == "candidate/0"
    assert prepared.name == "Spec Agent"
    assert prepared.work_body == 'writing spec for candidate 1/1 "Refactor"'
    assert prepared.prompt.kind is PromptKind.FOLLOW_UP
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_SPECS": (
            "#12 OPEN - First candidate\n#11 CLOSED - Second candidate"
        ),
        "CANDIDATE_RANK": "1",
        "CANDIDATE_TITLE": "Refactor",
    }
    assert github_port.recent_spec_calls == 1
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_builds_exact_no_candidate_report_payload_from_driver_step():
    github_port = _GithubPortStandIn(
        recent_specs=[
            {"number": 12, "state": "OPEN", "title": "First candidate"},
            {"number": 11, "state": "CLOSED", "title": "Second candidate"},
        ]
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_NO_CANDIDATE,
            session_namespace="main",
            display_name="Rejection Report Agent",
            work_body="filing no-candidate report",
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=True,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_NO_CANDIDATE
    assert prepared.session_namespace == "main"
    assert prepared.name == "Rejection Report Agent"
    assert prepared.work_body == "filing no-candidate report"
    assert prepared.prompt.kind is PromptKind.FOLLOW_UP
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_SPECS": (
            "#12 OPEN - First candidate\n#11 CLOSED - Second candidate"
        ),
        "CANDIDATE_RANK": "",
        "CANDIDATE_TITLE": "",
    }
    assert github_port.recent_spec_calls == 1
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_builds_exact_spec_payload_without_lookup_policy_flag():
    github_port = _GithubPortStandIn(
        recent_specs=[
            {"number": 12, "state": "OPEN", "title": "First candidate"},
            {"number": 11, "state": "CLOSED", "title": "Second candidate"},
        ]
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SPEC,
            session_namespace="main",
            display_name="Spec Agent",
            work_body="writing spec",
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            candidate=ImproveCandidate(rank=1, title="Refactor"),
        ),
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_SPEC
    assert prepared.session_namespace == "main"
    assert prepared.name == "Spec Agent"
    assert prepared.work_body == "writing spec"
    assert prepared.prompt.kind is PromptKind.FOLLOW_UP
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_SPECS": (
            "#12 OPEN - First candidate\n#11 CLOSED - Second candidate"
        ),
        "CANDIDATE_RANK": "1",
        "CANDIDATE_TITLE": "Refactor",
    }
    assert github_port.recent_spec_calls == 1


def test_prepare_improve_step_uses_exact_empty_recent_spec_message_for_spec_template():
    github_port = _GithubPortStandIn(recent_specs=[])

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SPEC,
            session_namespace="main",
            display_name="Spec Agent",
            work_body="body",
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            candidate=ImproveCandidate(rank=2, title="Deepen module"),
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_SPECS": "No recent improve specs found.",
        "CANDIDATE_RANK": "2",
        "CANDIDATE_TITLE": "Deepen module",
    }
    assert github_port.recent_spec_calls == 1


def test_prepare_improve_step_uses_exact_empty_recent_spec_message_for_no_candidate_template():
    github_port = _GithubPortStandIn(recent_specs=[])

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_NO_CANDIDATE,
            session_namespace="main",
            display_name="Rejection Report Agent",
            work_body="body",
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "RECENT_IMPROVE_SPECS": "No recent improve specs found.",
        "CANDIDATE_RANK": "",
        "CANDIDATE_TITLE": "",
    }
    assert github_port.recent_spec_calls == 1


def test_prepare_improve_step_uses_short_sid_only_for_issues():
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_TICKETS,
            session_namespace="main",
            display_name="Slice Agent",
            work_body="filing sub-issues",
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=False,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.recent_spec_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_resumed_scan_uses_empty_recent_prd_message():
    github_port = _GithubPortStandIn(
        recent_spec_error=AssertionError(
            "mid-phase scan retries must not refetch specs"
        )
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SCAN,
            session_namespace="main",
            display_name="Scan Agent",
            work_body="picking up to 2 improvements",
            kind=PromptKind.ROLE_PROMPT,
            short_sid="abcd1234",
            fetch_recent_spec_titles=False,
            candidate_budget=2,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_SCAN
    assert prepared.session_namespace == "main"
    assert prepared.name == "Scan Agent"
    assert prepared.work_body == "picking up to 2 improvements"
    assert prepared.prompt.kind is PromptKind.ROLE_PROMPT
    assert prepared.prompt.scope_args == {
        "RECENT_IMPROVE_SPEC_TITLES": "No recent improve specs found.",
        "CANDIDATE_BUDGET": "2",
    }
    assert github_port.recent_spec_calls == 0


def test_prepare_improve_step_issues_scope_contains_only_short_sid():
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_TICKETS,
            session_namespace="main",
            display_name="Slice Agent",
            work_body="filing sub-issues",
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=False,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.recent_spec_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_builds_issues_payload_from_driver_step_prd_handoff():
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_TICKETS,
            session_namespace="candidate/0",
            display_name="Tickets Agent",
            work_body='filing tickets for candidate 1/1 "Refactor"',
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=False,
            candidate=ImproveCandidate(rank=1, title="Refactor"),
        ),
        github_port=github_port,
    )

    assert prepared.prompt.template == PromptTemplate.IMPROVE_TICKETS
    assert prepared.session_namespace == "candidate/0"
    assert prepared.name == "Tickets Agent"
    assert prepared.work_body == 'filing tickets for candidate 1/1 "Refactor"'
    assert prepared.prompt.kind is PromptKind.FOLLOW_UP
    assert prepared.prompt.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
    }
    assert github_port.recent_spec_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_propagates_recent_improve_prd_lookup_failures():
    error = GithubNetworkError("transport error", cause=RuntimeError("boom"))
    github_port = _GithubPortStandIn(recent_spec_error=error)

    with pytest.raises(GithubNetworkError) as exc_info:
        prepare_improve_step(
            ImproveStepPreparationRequest(
                prompt_template=PromptTemplate.IMPROVE_SCAN,
                session_namespace="main",
                display_name="Scan Agent",
                work_body="picking up to 1 improvement",
                kind=PromptKind.ROLE_PROMPT,
                short_sid="abcd1234",
                fetch_recent_spec_titles=True,
                candidate_budget=1,
            ),
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
    assert step2.prompt_key == "02-spec.md"
    assert step2.candidate == ImproveCandidate(
        rank=4, title="My Feature", spec_number=None
    )


def test_prepare_improve_step_accepts_request_with_candidate(tmp_path: Path) -> None:
    """ImproveStepPreparationRequest with a candidate passes through prepare_improve_step unchanged."""
    candidate = ImproveCandidate(rank=1, title="Foo", spec_number=42)
    request = ImproveStepPreparationRequest(
        prompt_template=PromptTemplate.IMPROVE_TICKETS,
        session_namespace="candidate/0",
        display_name="Slice Agent",
        work_body="filing sub-issues",
        kind=PromptKind.FOLLOW_UP,
        short_sid="abcd1234",
        fetch_recent_spec_titles=False,
        candidate=candidate,
    )
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(request, github_port=github_port)

    assert prepared.prompt.template == PromptTemplate.IMPROVE_TICKETS
    assert prepared.prompt.scope_args == {"IMPROVE_SHORT_SID": "abcd1234"}


def test_prepare_improve_step_prd_without_candidate_fails_loudly():
    github_port = _GithubPortStandIn(recent_specs=[])

    with pytest.raises(PromptRenderError):
        prepare_improve_step(
            ImproveStepPreparationRequest(
                prompt_template=PromptTemplate.IMPROVE_SPEC,
                session_namespace="main",
                display_name="PRD Agent",
                work_body="writing PRD",
                kind=PromptKind.FOLLOW_UP,
                short_sid="abcd1234",
                # candidate intentionally omitted
            ),
            github_port=github_port,
        )


def test_prepare_improve_step_scan_without_candidate_budget_fails_to_render():
    github_port = _GithubPortStandIn()

    with pytest.raises(PromptRenderError):
        prepare_improve_step(
            ImproveStepPreparationRequest(
                prompt_template=PromptTemplate.IMPROVE_SCAN,
                session_namespace="main",
                display_name="Scan Agent",
                work_body="",
                kind=PromptKind.ROLE_PROMPT,
                short_sid="abcd1234",
                fetch_recent_spec_titles=True,
                # candidate_budget omitted — None by default
            ),
            github_port=github_port,
        )


def test_scan_agent_row_body_says_picking_up_to_n_improvements() -> None:
    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SCAN,
            session_namespace="main",
            display_name="Scan Agent",
            work_body="picking up to 3 improvements",
            kind=PromptKind.ROLE_PROMPT,
            short_sid="abcd1234",
            fetch_recent_spec_titles=True,
            candidate_budget=3,
        ),
        github_port=_GithubPortStandIn(),
    )

    assert prepared.work_body == "picking up to 3 improvements"


def test_scan_agent_row_body_with_budget_one_says_picking_1_improvement() -> None:
    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SCAN,
            session_namespace="main",
            display_name="Scan Agent",
            work_body="picking 1 improvement",
            kind=PromptKind.ROLE_PROMPT,
            short_sid="abcd1234",
            fetch_recent_spec_titles=True,
            candidate_budget=1,
        ),
        github_port=_GithubPortStandIn(),
    )

    assert prepared.work_body == "picking 1 improvement"


def test_spec_agent_name_and_body_from_driver_prd_step() -> None:
    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_SPEC,
            session_namespace="candidate/0",
            display_name="Spec Agent",
            work_body='writing spec for candidate 1/3 "Alpha"',
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=True,
            candidate=ImproveCandidate(rank=1, title="Alpha"),
        ),
        github_port=_GithubPortStandIn(),
    )

    assert prepared.name == "Spec Agent"
    assert prepared.work_body == 'writing spec for candidate 1/3 "Alpha"'


def test_tickets_agent_name_and_body_from_driver_issues_step() -> None:
    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_TICKETS,
            session_namespace="candidate/0",
            display_name="Tickets Agent",
            work_body='filing tickets for candidate 1/3 "Alpha"',
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=False,
            candidate=ImproveCandidate(rank=1, title="Alpha"),
        ),
        github_port=_GithubPortStandIn(),
    )

    assert prepared.name == "Tickets Agent"
    assert prepared.work_body == 'filing tickets for candidate 1/3 "Alpha"'


def test_prepare_improve_step_unsupported_template_raises_type_error():
    github_port = _GithubPortStandIn()

    with pytest.raises(TypeError):
        prepare_improve_step(
            ImproveStepPreparationRequest(
                prompt_template=PromptTemplate.IMPLEMENT_BEHAVIOR,
                session_namespace="main",
                display_name="Behavior Agent",
                work_body="body",
                kind=PromptKind.FOLLOW_UP,
                short_sid="abcd1234",
            ),
            github_port=github_port,
        )


def test_prepare_improve_step_tickets_with_fetch_flag_makes_no_github_call():
    github_port = _GithubPortStandIn(
        recent_spec_error=AssertionError("IMPROVE_TICKETS must never call github_port")
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_TICKETS,
            session_namespace="main",
            display_name="Tickets Agent",
            work_body="filing sub-issues",
            kind=PromptKind.FOLLOW_UP,
            short_sid="abcd1234",
            fetch_recent_spec_titles=True,
        ),
        github_port=github_port,
    )

    assert prepared.prompt.scope_args == {"IMPROVE_SHORT_SID": "abcd1234"}
    assert github_port.recent_spec_calls == 0
