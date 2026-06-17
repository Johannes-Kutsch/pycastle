import pytest

from pycastle.agents.output_protocol import IssueOutput
from pycastle.config import Config
from pycastle.diagnostic_issue_report_validation import (
    DiagnosticIssueReportValidationOutcome,
    validate_diagnostic_issue_report,
)


class RecordingFiledIssueReader:
    def __init__(self, issue: dict) -> None:
        self.issue = issue
        self.calls: list[int] = []

    def get_issue(self, number: int) -> dict:
        self.calls.append(number)
        return self.issue


def test_diagnostic_issue_report_validation_returns_hitl_without_reading_filed_issue():
    reader = RecordingFiledIssueReader({"body": "x" * 100})

    outcome = validate_diagnostic_issue_report(
        caller="Pre-Flight Reporter",
        issue_output=IssueOutput(number=41, labels=["bug", "ready-for-human"]),
        cfg=Config(),
        filed_issue_reader=reader,
    )

    assert outcome is DiagnosticIssueReportValidationOutcome.HITL
    assert reader.calls == []


def test_diagnostic_issue_report_validation_returns_afk_with_reported_label_fallback():
    reader = RecordingFiledIssueReader({"body": "x" * 100})

    outcome = validate_diagnostic_issue_report(
        caller="Pre-Flight Reporter",
        issue_output=IssueOutput(
            number=42,
            labels=["bug", "ready-for-agent", "behavior-slice"],
        ),
        cfg=Config(),
        filed_issue_reader=reader,
    )

    assert outcome is DiagnosticIssueReportValidationOutcome.AFK
    assert reader.calls == [42]


def test_diagnostic_issue_report_validation_raises_readiness_error_for_malformed_afk_issue():
    reader = RecordingFiledIssueReader(
        {"body": "x" * 100, "labels": ["behavior-slice", "docs-slice"]}
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "Pre-Flight Reporter filed issue #42 on the AFK branch without "
            "exactly one slice-mode label"
        ),
    ):
        validate_diagnostic_issue_report(
            caller="Pre-Flight Reporter",
            issue_output=IssueOutput(
                number=42,
                labels=["bug", "ready-for-agent", "behavior-slice"],
            ),
            cfg=Config(),
            filed_issue_reader=reader,
        )

    assert reader.calls == [42]
