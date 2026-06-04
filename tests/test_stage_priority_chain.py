from pycastle.config.types import StageOverride
from pycastle.service_availability import iter_stage_chain as iter_legacy_stage_chain
from pycastle.stage_priority_chain import (
    chain_entries,
    iter_stage_chain,
    referenced_service_names,
    render_chain_label,
    validation_labels,
)


def test_iter_stage_chain_yields_stage_chain_in_priority_order() -> None:
    tertiary = StageOverride(service="claude", model="haiku", effort="low")
    secondary = StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="low",
        fallback=tertiary,
    )
    primary = StageOverride(
        service="opencode",
        model="deepseek-v4-flash",
        effort="medium",
        fallback=secondary,
    )

    result = list(iter_stage_chain(primary))

    assert result == [primary, secondary, tertiary]
    assert result[0] is primary
    assert result[1] is secondary
    assert result[2] is tertiary


def test_service_availability_iter_stage_chain_stays_import_compatible() -> None:
    fallback = StageOverride(service="claude", model="sonnet", effort="medium")
    override = StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=fallback,
    )

    result = list(iter_legacy_stage_chain(override))

    assert result == [override, fallback]
    assert result[0] is override
    assert result[1] is fallback


def test_chain_entries_exposes_stage_override_chain_facts_in_priority_order() -> None:
    tertiary = StageOverride(service="claude", model="haiku", effort="low")
    secondary = StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="medium",
        fallback=tertiary,
    )
    primary = StageOverride(
        service="opencode",
        model="deepseek-v4-flash",
        effort="high",
        fallback=secondary,
    )

    result = chain_entries(primary)

    assert [entry.service for entry in result] == ["opencode", "codex", "claude"]
    assert [entry.model for entry in result] == [
        "deepseek-v4-flash",
        "gpt-5.4-mini",
        "haiku",
    ]
    assert [entry.effort for entry in result] == ["high", "medium", "low"]
    assert [entry.fallback for entry in result] == [secondary, tertiary, None]


def test_validation_labels_marks_primary_and_fallback_nodes_for_stage() -> None:
    override = StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
            fallback=StageOverride(service="opencode", model="kimi-k2.6", effort="low"),
        ),
    )

    result = validation_labels("implement", override)

    assert result == ("implement", "implement fallback", "implement fallback")


def test_validation_labels_returns_only_stage_name_for_single_node_chain() -> None:
    override = StageOverride(service="codex", model="gpt-5.4", effort="medium")

    result = validation_labels("review", override)

    assert result == ("review",)


def test_render_chain_label_joins_services_and_marks_missing_names() -> None:
    override = StageOverride(
        service="codex",
        fallback=StageOverride(service="", fallback=StageOverride(service="claude")),
    )

    result = render_chain_label(override)

    assert result == "codex -> <missing> -> claude"


def test_render_chain_label_marks_missing_primary_service_name() -> None:
    override = StageOverride(service="", model="gpt-5.4", effort="medium")

    result = render_chain_label(override)

    assert result == "<missing>"


def test_referenced_service_names_collects_primary_and_fallback_service_names() -> None:
    override = StageOverride(
        service="opencode",
        fallback=StageOverride(
            service="codex",
            fallback=StageOverride(service="claude"),
        ),
    )

    result = referenced_service_names(override)

    assert result == ("opencode", "codex", "claude")


def test_referenced_service_names_deduplicates_repeated_services() -> None:
    override = StageOverride(
        service="codex",
        fallback=StageOverride(
            service="claude",
            fallback=StageOverride(
                service="codex",
                fallback=StageOverride(service="opencode"),
            ),
        ),
    )

    result = referenced_service_names(override)

    assert result == ("codex", "claude", "opencode")


def test_referenced_service_names_excludes_empty_names_deterministically() -> None:
    override = StageOverride(
        service=" ",
        fallback=StageOverride(
            service="codex",
            fallback=StageOverride(
                service="",
                fallback=StageOverride(service="claude"),
            ),
        ),
    )

    first = referenced_service_names(override)
    second = referenced_service_names(override)

    assert first == ("codex", "claude")
    assert second == first
