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


@dataclasses.dataclass(frozen=True)
class Malformed:
    found: list[str]


SliceClassification = WellFormed | Malformed


BODY_FLOOR = 100


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
        return WellFormed(label_to_mode[matches[0]])
    return Malformed(found=matches)


def is_well_formed_body(issue: dict) -> bool:
    body = issue.get("body") or ""
    return len(body.strip()) >= BODY_FLOOR
