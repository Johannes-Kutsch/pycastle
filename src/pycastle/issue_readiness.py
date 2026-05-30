from __future__ import annotations

import dataclasses
import enum
from typing import TYPE_CHECKING

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
class IssueReadiness:
    slice_status: SliceClassification
    body_floor_status: BodyFloorClassification
    is_ready: bool
    selected_mode: SliceMode | None
    kind: IssueReadinessKind = dataclasses.field(
        default=IssueReadinessKind.MALFORMED,
        compare=False,
    )
    hitl_label: str | None = dataclasses.field(default=None, compare=False)
    is_hitl_exempt: bool = dataclasses.field(default=False, compare=False)


@dataclasses.dataclass(frozen=True)
class ClassifiedIssue:
    issue: dict
    readiness: IssueReadiness


def selected_mode_for_issue(issue: dict, cfg: Config) -> SliceMode | None:
    readiness = issue.get("readiness")
    if isinstance(readiness, IssueReadiness):
        if readiness.selected_mode is not None:
            return readiness.selected_mode
        if isinstance(readiness.slice_status, WellFormed):
            return readiness.slice_status.mode
        return None

    fallback = classify_issue_readiness(issue, cfg).slice_status
    if isinstance(fallback, WellFormed):
        return fallback.mode
    return None


def slice_labels(cfg: Config) -> frozenset[str]:
    return frozenset(
        {cfg.refactor_slice_label, cfg.behavior_slice_label, cfg.docs_slice_label}
    )


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


def is_well_formed_body(issue: dict) -> bool:
    body = issue.get("body") or ""
    return len(body.strip()) >= BODY_FLOOR


def classify_body_floor(issue: dict) -> BodyFloorClassification:
    stripped_length = len((issue.get("body") or "").strip())
    if stripped_length >= BODY_FLOOR:
        return WellFormedBody(stripped_length=stripped_length)
    return MalformedBody(stripped_length=stripped_length)


def classify_issue_readiness(issue: dict, cfg: Config) -> IssueReadiness:
    slice_status = classify_slice(issue, cfg)
    body_floor_status = classify_body_floor(issue)
    issue_labels: list[str] = issue.get("labels") or []
    is_ready = False
    selected_mode = None
    hitl_label = cfg.hitl_label if cfg.hitl_label in issue_labels else None
    is_hitl_exempt = hitl_label is not None
    kind = IssueReadinessKind.MALFORMED

    if is_hitl_exempt:
        kind = IssueReadinessKind.HITL_EXEMPT
    elif isinstance(slice_status, WellFormed) and isinstance(
        body_floor_status, WellFormedBody
    ):
        is_ready = True
        selected_mode = slice_status.mode
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

    return IssueReadiness(
        slice_status=slice_status,
        body_floor_status=body_floor_status,
        is_ready=is_ready,
        selected_mode=selected_mode,
        kind=kind,
        hitl_label=hitl_label,
        is_hitl_exempt=is_hitl_exempt,
    )


def classify_issues(issues: list[dict], cfg: Config) -> list[ClassifiedIssue]:
    return [
        ClassifiedIssue(issue=issue, readiness=classify_issue_readiness(issue, cfg))
        for issue in issues
    ]
