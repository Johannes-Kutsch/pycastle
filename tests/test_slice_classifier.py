from __future__ import annotations

import json

import pytest

from pycastle.agents.slice_classifier import (
    ConcreteSliceVerdict,
    SliceClassifierVerdict,
    UncertainSliceVerdict,
    parse_classifier_output,
)
from pycastle.issue_readiness import SliceMode

# ── Behavior 1: Concrete slice-mode verdicts ──────────────────────────────────


@pytest.mark.parametrize(
    ("mode_key", "expected_mode"),
    [
        ("behavior", SliceMode.BEHAVIOR),
        ("refactor", SliceMode.REFACTOR),
        ("docs", SliceMode.DOCS),
    ],
)
def test_concrete_mode_output_yields_concrete_verdict(mode_key, expected_mode):
    raw = json.dumps({"mode": mode_key})
    verdict = parse_classifier_output(raw)
    assert isinstance(verdict, ConcreteSliceVerdict)
    assert verdict.mode is expected_mode


# ── Behavior 2: Uncertainty verdict carries the model's reason ────────────────


def test_uncertain_output_yields_uncertain_verdict_with_reason():
    reason = "Cannot determine if changes are behavioral or docs-only."
    raw = json.dumps({"uncertain": True, "reason": reason})
    verdict = parse_classifier_output(raw)
    assert isinstance(verdict, UncertainSliceVerdict)
    assert verdict.reason == reason


# ── Behavior 3: Malformed/empty/unexpected output falls back safely ───────────


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "{}",
        '{"mode": "unknown_mode"}',
        '{"something": "else"}',
        "null",
        "42",
        '["behavior"]',
        '{"mode": "BEHAVIOR"}',
        '{"mode": "Refactor"}',
        '{"mode": 1}',
        '{"reason": "   "}',
        '{"reason": null}',
    ],
)
def test_malformed_or_unexpected_output_yields_uncertain_verdict(raw):
    verdict = parse_classifier_output(raw)
    assert isinstance(verdict, UncertainSliceVerdict)


def test_whitespace_only_reason_uses_fallback_not_whitespace():
    verdict = parse_classifier_output('{"reason": "   "}')
    assert isinstance(verdict, UncertainSliceVerdict)
    assert verdict.reason.strip() != ""


def test_uncertain_reason_is_stripped():
    verdict = parse_classifier_output('{"reason": "  leading and trailing  "}')
    assert isinstance(verdict, UncertainSliceVerdict)
    assert verdict.reason == "leading and trailing"


# ── Behavior 4: Verdict type is shared ───────────────────────────────────────


def test_both_verdict_variants_are_importable_from_slice_classifier():
    # Both concrete and uncertain verdicts come from the same module, so
    # downstream label logic and classifier invocation share the same type.
    concrete: SliceClassifierVerdict = ConcreteSliceVerdict(mode=SliceMode.BEHAVIOR)
    uncertain: SliceClassifierVerdict = UncertainSliceVerdict(reason="unclear")
    assert isinstance(concrete, ConcreteSliceVerdict)
    assert isinstance(uncertain, UncertainSliceVerdict)


def test_parse_classifier_output_returns_slice_classifier_verdict_for_any_input():
    # parse_classifier_output always returns a SliceClassifierVerdict regardless
    # of whether the input is concrete or uncertain.
    concrete_verdict = parse_classifier_output(json.dumps({"mode": "refactor"}))
    uncertain_verdict = parse_classifier_output(
        json.dumps({"uncertain": True, "reason": "not sure"})
    )
    assert isinstance(concrete_verdict, ConcreteSliceVerdict | UncertainSliceVerdict)
    assert isinstance(uncertain_verdict, ConcreteSliceVerdict | UncertainSliceVerdict)
