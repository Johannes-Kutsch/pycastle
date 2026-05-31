import dataclasses
from typing import Literal, TypeAlias


LabelActionIntent: TypeAlias = Literal["add", "remove"]


@dataclasses.dataclass(frozen=True)
class LabelSyncAction:
    issue_number: int
    label_name: str
    intent: LabelActionIntent
    comment_body: str | None = None


@dataclasses.dataclass(frozen=True)
class BlockerSummaryInputs:
    malformed_slice_mode_issues: tuple[dict, ...] = ()
    malformed_body_issues: tuple[dict, ...] = ()


@dataclasses.dataclass(frozen=True)
class PlanningReadinessResult:
    ready_candidates: tuple[dict, ...] = ()
    malformed_body_issues: tuple[dict, ...] = ()
    malformed_slice_mode_issues: tuple[dict, ...] = ()
    label_sync_actions: tuple[LabelSyncAction, ...] = ()
    blocker_summary_inputs: BlockerSummaryInputs = dataclasses.field(
        default_factory=BlockerSummaryInputs
    )
