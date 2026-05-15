from __future__ import annotations

import dataclasses
import enum
from typing import TYPE_CHECKING

from .prompt_pipeline import PromptTemplate

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


def slice_labels(cfg: Config) -> frozenset[str]:
    return frozenset(
        {cfg.refactor_slice_label, cfg.behavior_slice_label, cfg.docs_slice_label}
    )


def classify_slice(issue: dict, cfg: Config) -> SliceClassification:
    all_labels = slice_labels(cfg)
    issue_labels: list[str] = issue.get("labels") or []
    matches = [lbl for lbl in issue_labels if lbl in all_labels]
    if len(matches) == 1:
        label = matches[0]
        if label == cfg.refactor_slice_label:
            return WellFormed(SliceMode.REFACTOR)
        elif label == cfg.behavior_slice_label:
            return WellFormed(SliceMode.BEHAVIOR)
        else:
            return WellFormed(SliceMode.DOCS)
    return Malformed(found=matches)
