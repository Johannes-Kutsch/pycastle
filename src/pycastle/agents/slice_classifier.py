from __future__ import annotations

import dataclasses
import json

from pycastle.issue_readiness import SliceMode

_MODE_MAP: dict[str, SliceMode] = {
    "behavior": SliceMode.BEHAVIOR,
    "refactor": SliceMode.REFACTOR,
    "docs": SliceMode.DOCS,
}

_FALLBACK_REASON = "Model output was malformed or did not name a recognised slice mode."


@dataclasses.dataclass(frozen=True)
class ConcreteSliceVerdict:
    mode: SliceMode


@dataclasses.dataclass(frozen=True)
class UncertainSliceVerdict:
    reason: str


type SliceClassifierVerdict = ConcreteSliceVerdict | UncertainSliceVerdict


def parse_classifier_output(raw: str) -> SliceClassifierVerdict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return UncertainSliceVerdict(reason=_FALLBACK_REASON)

    if not isinstance(data, dict):
        return UncertainSliceVerdict(reason=_FALLBACK_REASON)

    mode_key = data.get("mode")
    if isinstance(mode_key, str) and mode_key in _MODE_MAP:
        return ConcreteSliceVerdict(mode=_MODE_MAP[mode_key])

    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        return UncertainSliceVerdict(reason=reason.strip())

    return UncertainSliceVerdict(reason=_FALLBACK_REASON)


__all__ = [
    "ConcreteSliceVerdict",
    "SliceClassifierVerdict",
    "UncertainSliceVerdict",
    "parse_classifier_output",
]
