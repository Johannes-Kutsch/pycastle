import pytest

from pycastle.agents.output_protocol import IssueOutput
from pycastle.config import Config
from pycastle.diagnostic_issue_report_validation import (
    DiagnosticIssueReportValidationAFK,
    DiagnosticIssueReportValidationHITL,
    validate_diagnostic_issue_report,
)


class RecordingFiledIssueReader:
    def __init__(self, issue: dict) -> None:
        self.issue = issue
        self.calls: list[int] = []

    def get_issue(self, number: int) -> dict:
        self.calls.append(number)
        return self.issue


def test_diagnostic_issue_report_validation_returns_typed_hitl_for_configured_label_without_reading_filed_issue():
    reader = RecordingFiledIssueReader({"body": "x" * 100})

    outcome = validate_diagnostic_issue_report(
        caller="Pre-Flight Reporter",
        issue_output=IssueOutput(number=41, labels=["bug", "needs-human"]),
        cfg=Config(hitl_label="needs-human"),
        filed_issue_reader=reader,
    )

    assert outcome == DiagnosticIssueReportValidationHITL(issue_number=41)
    assert reader.calls == []


def test_diagnostic_issue_report_validation_returns_hitl_without_reading_filed_issue():
    reader = RecordingFiledIssueReader({"body": "x" * 100})

    outcome = validate_diagnostic_issue_report(
        caller="Pre-Flight Reporter",
        issue_output=IssueOutput(number=41, labels=["bug", "ready-for-human"]),
        cfg=Config(),
        filed_issue_reader=reader,
    )

    assert outcome == DiagnosticIssueReportValidationHITL(issue_number=41)
    assert reader.calls == []


def test_diagnostic_issue_report_validation_returns_typed_afk_when_filed_issue_is_ready():
    reader = RecordingFiledIssueReader(
        {"body": "x" * 100, "labels": ["bug", "behavior-slice"]}
    )

    outcome = validate_diagnostic_issue_report(
        caller="Pre-Flight Reporter",
        issue_output=IssueOutput(
            number=42,
            labels=["bug", "ready-for-agent", "behavior-slice"],
        ),
        cfg=Config(),
        filed_issue_reader=reader,
    )

    assert outcome == DiagnosticIssueReportValidationAFK(issue_number=42)
    assert reader.calls == [42]


def test_diagnostic_issue_report_validation_raises_when_filed_issue_is_missing_slice_mode_labels():
    reader = RecordingFiledIssueReader({"body": "x" * 100})

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


def test_diagnostic_issue_report_validation_raises_when_filed_issue_body_is_below_floor():
    reader = RecordingFiledIssueReader(
        {"body": "short", "labels": ["bug", "behavior-slice"]}
    )

    with pytest.raises(
        RuntimeError,
        match="whose body is below the minimum length floor",
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
