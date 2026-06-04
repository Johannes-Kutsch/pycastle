from __future__ import annotations

import dataclasses
import enum
from typing import TYPE_CHECKING, Literal

from .prompts.pipeline import PromptTemplate

if TYPE_CHECKING:
    from .config import Config


class SliceMode(enum.Enum):
    REFACTOR = ("refactor", PromptTemplate.IMPLEMENT_REFACTOR)
    BEHAVIOR = ("behavior", PromptTemplate.IMPLEMENT_BEHAVIOR)
    DOCS = ("docs", PromptTemplate.IMPLEMENT_DOCS)

    @property
    def display_name(self) -> str:
        return self.value[0]  # type: ignore[index]

    @property
    def template(self) -> PromptTemplate:
        return self.value[1]  # type: ignore[index]


@dataclasses.dataclass(frozen=True)
class WellFormed:
    mode: SliceMode
    label: str | None = None


@dataclasses.dataclass(frozen=True)
class Malformed:
    found: list[str]
    configured: frozenset[str] = dataclasses.field(default_factory=frozenset)


SliceClassification = WellFormed | Malformed


BODY_FLOOR = 100


class IssueReadinessKind(enum.Enum):
    READY_AFK = "ready_afk"
    HITL_EXEMPT = "hitl_exempt"
    MISSING_SLICE_MODE = "missing_slice_mode"
    MULTIPLE_SLICE_MODES = "multiple_slice_modes"
    SHORT_BODY = "short_body"
    MALFORMED = "malformed"


@dataclasses.dataclass(frozen=True)
class WellFormedBody:
    stripped_length: int
    body_floor: int = BODY_FLOOR


@dataclasses.dataclass(frozen=True)
class MalformedBody:
    stripped_length: int
    body_floor: int = BODY_FLOOR


BodyFloorClassification = WellFormedBody | MalformedBody


@dataclasses.dataclass(frozen=True)
class ReadyIssueOutcome:
    display_name: str
    template: PromptTemplate


@dataclasses.dataclass(frozen=True)
class MarkerLabelDecision:
    label_name: str
    intent: Literal["add"] = "add"


@dataclasses.dataclass(frozen=True)
class AFKReadyOutcome:
    slice_mode_display_name: str
    implement_template: PromptTemplate


@dataclasses.dataclass(frozen=True)
class AFKBlockedOutcome:
    current_slice_labels: tuple[str, ...]
    marker_decisions: tuple[MarkerLabelDecision, ...]
    stripped_body_length: int
    body_floor: int
    has_invalid_slice_mode: bool = False
    has_short_body: bool = False


@dataclasses.dataclass(frozen=True)
class BlockedIssueOutcome:
    slice_status: SliceClassification
    body_floor_status: BodyFloorClassification


@dataclasses.dataclass(frozen=True)
class IssueReadiness:
    slice_status: SliceClassification
    body_floor_status: BodyFloorClassification
    is_ready: bool
    selected_mode: SliceMode | None
    ready: ReadyIssueOutcome | None = dataclasses.field(default=None, compare=False)
    blocked: BlockedIssueOutcome | None = dataclasses.field(default=None, compare=False)
    kind: IssueReadinessKind = dataclasses.field(
        default=IssueReadinessKind.MALFORMED,
        compare=False,
    )
    hitl_label: str | None = dataclasses.field(default=None, compare=False)
    is_hitl_exempt: bool = dataclasses.field(default=False, compare=False)


def resolve_issue_readiness(issue: dict, cfg: Config) -> IssueReadiness:
    carried = issue.get("readiness")
    if isinstance(carried, IssueReadiness):
        return carried
    return classify_issue_readiness(issue, cfg)


def evaluate_issue_afk_readiness(
    issue: dict, cfg: Config
) -> AFKReadyOutcome | AFKBlockedOutcome:
    readiness = resolve_issue_readiness(issue, cfg)
    ready_mode: SliceMode | None = None
    if readiness.ready is not None:
        ready_mode = readiness.selected_mode
        if ready_mode is None and isinstance(readiness.slice_status, WellFormed):
            ready_mode = readiness.slice_status.mode
    elif readiness.is_ready:
        ready_mode = readiness.selected_mode
        if ready_mode is None and isinstance(readiness.slice_status, WellFormed):
            ready_mode = readiness.slice_status.mode

    if ready_mode is not None:
        return AFKReadyOutcome(
            slice_mode_display_name=ready_mode.display_name,
            implement_template=ready_mode.template,
        )

    current_slice_labels: tuple[str, ...] = ()
    if isinstance(readiness.slice_status, Malformed):
        current_slice_labels = tuple(readiness.slice_status.found)

    marker_decisions: list[MarkerLabelDecision] = []
    has_invalid_slice_mode = False
    if readiness.kind in {
        IssueReadinessKind.MISSING_SLICE_MODE,
        IssueReadinessKind.MULTIPLE_SLICE_MODES,
        IssueReadinessKind.MALFORMED,
    }:
        has_invalid_slice_mode = True
        marker_decisions.append(
            MarkerLabelDecision(label_name=cfg.needs_slice_type_label)
        )
    has_short_body = False
    if isinstance(readiness.body_floor_status, MalformedBody):
        has_short_body = True
        marker_decisions.append(MarkerLabelDecision(label_name=cfg.needs_info_label))

    return AFKBlockedOutcome(
        current_slice_labels=current_slice_labels,
        marker_decisions=tuple(marker_decisions),
        stripped_body_length=readiness.body_floor_status.stripped_length,
        body_floor=readiness.body_floor_status.body_floor,
        has_invalid_slice_mode=has_invalid_slice_mode,
        has_short_body=has_short_body,
    )


def selected_mode_for_issue(issue: dict, cfg: Config) -> SliceMode | None:
    readiness = resolve_issue_readiness(issue, cfg)
    if readiness.selected_mode is not None:
        return readiness.selected_mode
    if isinstance(readiness.slice_status, WellFormed):
        return readiness.slice_status.mode
    return None


def diagnostic_issue_readiness_error(
    *,
    caller: str,
    issue_number: int,
    issue_labels: list[str] | tuple[str, ...],
    readiness: IssueReadiness,
) -> str | None:
    if readiness.kind in {
        IssueReadinessKind.MISSING_SLICE_MODE,
        IssueReadinessKind.MULTIPLE_SLICE_MODES,
        IssueReadinessKind.MALFORMED,
    }:
        malformed = readiness.slice_status
        if not isinstance(malformed, WellFormed):
            return (
                f"{caller} filed issue #{issue_number} on the AFK branch "
                f"without exactly one slice-mode label — got labels={issue_labels!r}. "
                f"Expected exactly one of {sorted(malformed.configured)!r}."
            )
    if readiness.kind in {
        IssueReadinessKind.SHORT_BODY,
        IssueReadinessKind.MALFORMED,
    }:
        return (
            f"{caller} filed issue #{issue_number} whose body is "
            f"below the minimum length floor — body too short to be valid."
        )
    return None


def classify_slice(issue: dict, cfg: Config) -> SliceClassification:
    label_to_mode = {
        cfg.refactor_slice_label: SliceMode.REFACTOR,
        cfg.behavior_slice_label: SliceMode.BEHAVIOR,
        cfg.docs_slice_label: SliceMode.DOCS,
    }
    issue_labels: list[str] = issue.get("labels") or []
    matches = [lbl for lbl in issue_labels if lbl in label_to_mode]
    if len(matches) == 1:
        return WellFormed(label_to_mode[matches[0]], label=matches[0])
    return Malformed(found=matches, configured=frozenset(label_to_mode))


def _classify_body_floor(issue: dict) -> BodyFloorClassification:
    stripped_length = len((issue.get("body") or "").strip())
    if stripped_length >= BODY_FLOOR:
        return WellFormedBody(stripped_length=stripped_length)
    return MalformedBody(stripped_length=stripped_length)


def classify_issue_readiness(issue: dict, cfg: Config) -> IssueReadiness:
    slice_status = classify_slice(issue, cfg)
    body_floor_status = _classify_body_floor(issue)
    issue_labels: list[str] = issue.get("labels") or []
    is_ready = False
    selected_mode = None
    hitl_label = cfg.hitl_label if cfg.hitl_label in issue_labels else None
    is_hitl_exempt = hitl_label is not None
    kind = IssueReadinessKind.MALFORMED
    ready = None
    blocked = None

    if is_hitl_exempt:
        kind = IssueReadinessKind.HITL_EXEMPT
    elif isinstance(slice_status, WellFormed) and isinstance(
        body_floor_status, WellFormedBody
    ):
        is_ready = True
        selected_mode = slice_status.mode
        ready = ReadyIssueOutcome(
            display_name=slice_status.mode.display_name,
            template=slice_status.mode.template,
        )
        kind = IssueReadinessKind.READY_AFK
    elif isinstance(slice_status, Malformed):
        if isinstance(body_floor_status, MalformedBody):
            kind = IssueReadinessKind.MALFORMED
        elif slice_status.found:
            kind = IssueReadinessKind.MULTIPLE_SLICE_MODES
        else:
            kind = IssueReadinessKind.MISSING_SLICE_MODE
    else:
        kind = IssueReadinessKind.SHORT_BODY

    if not is_ready and not is_hitl_exempt:
        blocked = BlockedIssueOutcome(
            slice_status=slice_status,
            body_floor_status=body_floor_status,
        )

    return IssueReadiness(
        slice_status=slice_status,
        body_floor_status=body_floor_status,
        is_ready=is_ready,
        selected_mode=selected_mode,
        ready=ready,
        blocked=blocked,
        kind=kind,
        hitl_label=hitl_label,
        is_hitl_exempt=is_hitl_exempt,
    )
