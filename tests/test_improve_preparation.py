from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pycastle.iteration.improve import ImprovePhaseDriver
from pycastle.iteration.improve_preparation import (
    ImproveStepPreparationRequest,
    prepare_improve_step,
)
from pycastle.prompts.pipeline import PromptTemplate
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
        prd_number=None,
        github_port=github_port,
    )

    assert prepared.prompt_template == PromptTemplate.IMPROVE_SCAN
    assert prepared.session_namespace == "main"
    assert prepared.display_name == "Scan Agent"
    assert prepared.work_body == "picking an improvement"
    assert prepared.send_role_prompt_on_resume is False
    assert prepared.scope_args == {
        "RECENT_IMPROVE_PRD_TITLES": "#12 OPEN - First candidate"
    }
    assert github_port.recent_prd_calls == 1


def test_prepare_improve_step_uses_empty_issue_placeholders_without_prd_number():
    github_port = _GithubPortStandIn()

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_ISSUES,
            session_namespace="issues",
            display_name="Slice Agent",
            work_body="filing sub-issues",
            send_role_prompt_on_resume=True,
            short_sid="abcd1234",
            prd_number=None,
            fetch_recent_prd_titles=False,
        ),
        github_port=github_port,
    )

    assert prepared.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "ISSUE_NUMBER": "",
        "ISSUE_TITLE": "",
        "ISSUE_BODY": "",
        "ISSUE_COMMENTS": "",
    }
    assert github_port.recent_prd_calls == 0
    assert github_port.issue_calls == []
    assert github_port.issue_comment_calls == []


def test_prepare_improve_step_resumed_scan_uses_empty_recent_prd_message(
    tmp_path: Path,
):
    driver_dir = tmp_path / "improve"
    driver_dir.mkdir(parents=True, exist_ok=True)
    (driver_dir / "_phase_in_flight").write_text("01-scan", encoding="utf-8")
    driver = ImprovePhaseDriver(driver_dir, no_candidate_report=True)
    step = driver.start()

    assert step is not None
    github_port = _GithubPortStandIn(
        recent_prd_error=AssertionError("mid-phase scan retries must not refetch PRDs")
    )

    prepared = prepare_improve_step(
        step,
        short_sid="abcd1234",
        prd_number=None,
        github_port=github_port,
    )

    assert prepared.prompt_template == PromptTemplate.IMPROVE_SCAN
    assert prepared.session_namespace == "main"
    assert prepared.display_name == "Scan Agent"
    assert prepared.work_body == "picking an improvement"
    assert prepared.send_role_prompt_on_resume is False
    assert prepared.scope_args == {
        "RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."
    }
    assert github_port.recent_prd_calls == 0


def test_prepare_improve_step_reads_issue_and_comments_for_issues_scope():
    github_port = _GithubPortStandIn(
        issue={"number": 77, "title": "Improve PRD", "body": "PRD body"},
        comments=[
            {
                "author": "alice",
                "created_at": "2026-01-01T00:00:00Z",
                "body": "looks good",
            }
        ],
    )

    prepared = prepare_improve_step(
        ImproveStepPreparationRequest(
            prompt_template=PromptTemplate.IMPROVE_ISSUES,
            session_namespace="issues",
            display_name="Slice Agent",
            work_body="filing sub-issues",
            send_role_prompt_on_resume=True,
            short_sid="abcd1234",
            prd_number=77,
            fetch_recent_prd_titles=False,
        ),
        github_port=github_port,
    )

    assert prepared.scope_args == {
        "IMPROVE_SHORT_SID": "abcd1234",
        "ISSUE_NUMBER": "77",
        "ISSUE_TITLE": "Improve PRD",
        "ISSUE_BODY": "PRD body",
        "ISSUE_COMMENTS": "## Comment by @alice at 2026-01-01T00:00:00Z\n\nlooks good",
    }
    assert github_port.recent_prd_calls == 0
    assert github_port.issue_calls == [77]
    assert github_port.issue_comment_calls == [77]


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
            prd_number=None,
            github_port=github_port,
        )

    assert exc_info.value is error


def test_prepare_improve_step_propagates_issue_read_failures():
    error = GithubNetworkError("transport error", cause=RuntimeError("boom"))
    github_port = _GithubPortStandIn(issue_error=error)

    with pytest.raises(GithubNetworkError) as exc_info:
        prepare_improve_step(
            ImproveStepPreparationRequest(
                prompt_template=PromptTemplate.IMPROVE_ISSUES,
                session_namespace="issues",
                display_name="Slice Agent",
                work_body="filing sub-issues",
                send_role_prompt_on_resume=True,
                short_sid="abcd1234",
                prd_number=42,
                fetch_recent_prd_titles=False,
            ),
            github_port=github_port,
        )

    assert exc_info.value is error
