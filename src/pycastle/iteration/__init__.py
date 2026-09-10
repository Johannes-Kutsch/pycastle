from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pycastle.agent_credential_failure_routing import (
    route_agent_credential_failure as route_agent_credential_failure,
)
from pycastle.bug_reporter import BUG_REPORT_LABEL_LIST as BUG_REPORT_LABEL_LIST
from pycastle.bug_reporter import auto_file_issue as auto_file_issue
from pycastle.display.rows import StatusRow as StatusRow
from pycastle.display.rows import StatusRowConfig as StatusRowConfig
from pycastle.display.rows import status_row as status_row
from pycastle.execution_contracts import CancellationToken as CancellationToken
from pycastle.iteration.improve import ImproveContinue as ImproveContinue
from pycastle.iteration.improve import ImproveNoCandidate as ImproveNoCandidate
from pycastle.iteration.in_flight import (
    select_in_flight_issues as select_in_flight_issues,
)
from pycastle.iteration.planning import AllBlocked as AllBlocked
from pycastle.iteration.planning import PlanReady as PlanReady
from pycastle.iteration.preflight import PreflightCache as PreflightCache

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Continue:
    pass


@dataclasses.dataclass(frozen=True)
class AbortedHITL:
    issue_number: int


@dataclasses.dataclass(frozen=True)
class AbortedUsageLimit:
    reset_time: datetime | None = None
    provider: str | None = None
    raw_message: str | None = None
    account_label: str | None = None
    is_permanent: bool = False
    stage_key: str | None = None


@dataclasses.dataclass(frozen=True)
class AbortedModelNotAvailable:
    service: str | None = None
    model: str | None = None
    stage_key: str | None = None


@dataclasses.dataclass(frozen=True)
class NoCandidate:
    pass


@dataclasses.dataclass(frozen=True)
class AbortedAgentFailure:
    failed_role: str
    issue_number: int | None = None


@dataclasses.dataclass(frozen=True)
class AbortedTimeout:
    failed_role: str
    worktree_path: Path


@dataclasses.dataclass(frozen=True)
class AbortedHardApiError:
    status_code: int | None


@dataclasses.dataclass(frozen=True)
class AbortedAgentCredentialFailure:
    status_code: int | None


@dataclasses.dataclass(frozen=True)
class AbortedSetup:
    phase: str
    message: str
    command: str | None = None
    output: str | None = None


@dataclasses.dataclass(frozen=True)
class Done:
    improve_cap_reached: bool = False


@dataclasses.dataclass(frozen=True)
class AbortedOperatorActionable:
    op: str
    stderr: str
    attempt_count: int


@dataclasses.dataclass(frozen=True)
class MergeCloseFailure:
    filed_issue_numbers: list[int]


type IterationOutcome = (
    Continue
    | Done
    | AbortedHITL
    | AbortedUsageLimit
    | AbortedModelNotAvailable
    | NoCandidate
    | AbortedAgentFailure
    | AbortedTimeout
    | AbortedHardApiError
    | AbortedAgentCredentialFailure
    | AbortedSetup
    | AbortedOperatorActionable
    | MergeCloseFailure
)

_FILED_USAGE_LIMIT_RAW_MESSAGES: set[str] = set()

from pycastle.iteration._run import (
    _route_and_abort_agent_credential_failure as _route_and_abort_agent_credential_failure,
)
from pycastle.iteration._run import (
    run_iteration as run_iteration,
)
