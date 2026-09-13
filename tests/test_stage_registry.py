from __future__ import annotations

import dataclasses

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.config.loader import Config
from pycastle.stage_registry import (
    STAGES,
    Stage,
    iter_stage_overrides,
    override_for_stage_key,
    stage_for_role,
    stage_key_for_role,
)

# --- STAGES sequence ---


def test_stages_covers_exactly_six_stages() -> None:
    assert len(STAGES) == 6


def test_stages_canonical_order() -> None:
    assert [s.stage_key for s in STAGES] == [
        "plan",
        "implement",
        "review",
        "merge",
        "preflight_issue",
        "improve",
    ]


def test_stage_records_are_frozen() -> None:
    stage = STAGES[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        stage.stage_key = "mutated"  # type: ignore[misc]


def test_stage_config_attrs_match_expected() -> None:
    expected = {
        "plan": "plan_override",
        "implement": "implement_override",
        "review": "review_override",
        "merge": "merge_override",
        "preflight_issue": "preflight_issue_override",
        "improve": "improve_override",
    }
    for stage in STAGES:
        assert stage.config_attr == expected[stage.stage_key]


# --- stage_for_role ---


@pytest.mark.parametrize(
    ("role", "expected_key"),
    [
        (AgentRole.PLANNER, "plan"),
        (AgentRole.IMPLEMENTER, "implement"),
        (AgentRole.REVIEWER, "review"),
        (AgentRole.MERGER, "merge"),
        (AgentRole.PREFLIGHT_ISSUE, "preflight_issue"),
        (AgentRole.IMPROVE, "improve"),
        (AgentRole.FAILURE_REPORT, "preflight_issue"),
    ],
)
def test_stage_for_role_returns_correct_stage(
    role: AgentRole, expected_key: str
) -> None:
    result = stage_for_role(role)
    assert isinstance(result, Stage)
    assert result.stage_key == expected_key


def test_stage_for_role_returns_none_for_unstaged_role() -> None:
    assert stage_for_role(AgentRole.DIVERGENCE_RESOLVER) is None


# --- stage_key_for_role ---


@pytest.mark.parametrize(
    ("role", "expected_key"),
    [
        (AgentRole.PLANNER, "plan"),
        (AgentRole.IMPLEMENTER, "implement"),
        (AgentRole.REVIEWER, "review"),
        (AgentRole.MERGER, "merge"),
        (AgentRole.PREFLIGHT_ISSUE, "preflight_issue"),
        (AgentRole.IMPROVE, "improve"),
    ],
)
def test_stage_key_for_role_returns_expected_key(
    role: AgentRole, expected_key: str
) -> None:
    assert stage_key_for_role(role) == expected_key


def test_stage_key_for_role_failure_report_shares_preflight_issue_stage() -> None:
    assert stage_key_for_role(AgentRole.FAILURE_REPORT) == "preflight_issue"


def test_stage_key_for_role_returns_none_for_divergence_resolver() -> None:
    assert stage_key_for_role(AgentRole.DIVERGENCE_RESOLVER) is None


def test_failure_report_and_preflight_issue_share_same_stage_object() -> None:
    assert stage_for_role(AgentRole.FAILURE_REPORT) is stage_for_role(
        AgentRole.PREFLIGHT_ISSUE
    )


# --- override_for_stage_key ---


@pytest.mark.parametrize(
    "key",
    ["plan", "implement", "review", "merge", "preflight_issue", "improve"],
)
def test_override_for_stage_key_returns_config_attr(key: str) -> None:
    cfg = Config()
    result = override_for_stage_key(cfg, key)
    assert result is getattr(cfg, f"{key}_override")


def test_override_for_stage_key_returns_none_for_unknown_key() -> None:
    cfg = Config()
    assert override_for_stage_key(cfg, "nonexistent") is None


def test_override_for_stage_key_returns_none_for_empty_string() -> None:
    cfg = Config()
    assert override_for_stage_key(cfg, "") is None


# --- iter_stage_overrides ---


def test_iter_stage_overrides_yields_six_pairs() -> None:
    cfg = Config()
    pairs = list(iter_stage_overrides(cfg))
    assert len(pairs) == 6


def test_iter_stage_overrides_yields_correct_keys_in_order() -> None:
    cfg = Config()
    keys = [key for key, _ in iter_stage_overrides(cfg)]
    assert keys == [
        "plan",
        "implement",
        "review",
        "merge",
        "preflight_issue",
        "improve",
    ]


def test_iter_stage_overrides_yields_same_objects_as_config_attrs() -> None:
    cfg = Config()
    pairs = list(iter_stage_overrides(cfg))
    for key, override in pairs:
        assert override is getattr(cfg, f"{key}_override")
