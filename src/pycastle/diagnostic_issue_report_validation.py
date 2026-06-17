from __future__ import annotations

import dataclasses
from typing import Protocol

from .agents.output_protocol import IssueOutput
from .config import Config
from .issue_readiness import issue_readiness_error_for_issue, resolve_issue_readiness


@dataclasses.dataclass(frozen=True)
class DiagnosticIssueReportValidationAFK:
    issue_number: int


@dataclasses.dataclass(frozen=True)
class DiagnosticIssueReportValidationHITL:
    issue_number: int


DiagnosticIssueReportValidationOutcome = (
    DiagnosticIssueReportValidationAFK | DiagnosticIssueReportValidationHITL
)


class FiledIssueReader(Protocol):
    """Read the filed issue by number after label classification permits it."""

    def get_issue(self, number: int) -> dict: ...


def validate_diagnostic_issue_report(
    *,
    caller: str,
    issue_output: IssueOutput,
    cfg: Config,
    filed_issue_reader: FiledIssueReader,
) -> DiagnosticIssueReportValidationOutcome:
    """Validate a diagnostic issue report.

    Reported HITL labels are classified before any filed-issue read.
    Malformed AFK issues raise readiness errors through the existing issue
    readiness path after reading the filed issue by number.
    """

    reported_readiness = resolve_issue_readiness(
        {"labels": list(issue_output.labels)},
        cfg,
    )
    if reported_readiness.is_hitl_exempt:
        return DiagnosticIssueReportValidationHITL(issue_number=issue_output.number)

    filed_issue = filed_issue_reader.get_issue(issue_output.number)
    filed_issue_with_labels = {
        **filed_issue,
        "number": issue_output.number,
    }
    readiness_error = issue_readiness_error_for_issue(
        caller=caller,
        issue=filed_issue_with_labels,
        cfg=cfg,
    )
    if readiness_error is not None:
        raise RuntimeError(readiness_error)
    return DiagnosticIssueReportValidationAFK(issue_number=issue_output.number)
