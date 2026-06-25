from pycastle.config.types import StageOverride
from pycastle.stage_priority_chain import (
    StageOverrideChain,
    chain_entries,
    iter_stage_chain,
    referenced_service_names,
    render_chain_label,
    select_configured_candidate_chain,
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


def test_stage_override_chain_keeps_validation_labels_and_chain_label_coherent() -> (
    None
):
    override = StageOverride(
        service="",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
    )

    result = StageOverrideChain(
        override=override,
        stage_name="plan",
        configured_service_names=("codex",),
        available_service_names=("codex",),
    )

    assert [entry.service for entry in result.entries] == ["", "claude"]
    assert result.validation_labels == ("plan", "plan fallback")
    assert result.rendered_chain_label == "<missing> -> claude"
    assert result.has_configured_candidate is False


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


def test_select_configured_candidate_chain_skips_unconfigured_primary() -> None:
    override = StageOverride(
        service="opencode",
        model="deepseek-v4-flash",
        effort="high",
        fallback=StageOverride(
            service="codex",
            model="gpt-5.4-mini",
            effort="medium",
            fallback=StageOverride(
                service="claude",
                model="haiku",
                effort="low",
            ),
        ),
    )

    result = select_configured_candidate_chain(
        override,
        configured_service_names=("codex", "claude"),
        available_service_names=("codex", "claude"),
    )

    assert result.has_configured_candidate is True
    assert result.selected_chain == StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="medium",
        fallback=StageOverride(
            service="claude",
            model="haiku",
            effort="low",
        ),
    )


def test_select_configured_candidate_chain_returns_first_configured_chain_when_all_are_exhausted() -> (
    None
):
    override = StageOverride(
        service="opencode",
        model="deepseek-v4-flash",
        effort="high",
        fallback=StageOverride(
            service="codex",
            model="gpt-5.4-mini",
            effort="medium",
            fallback=StageOverride(
                service="claude",
                model="haiku",
                effort="low",
            ),
        ),
    )

    result = select_configured_candidate_chain(
        override,
        configured_service_names=("codex", "claude"),
        available_service_names=(),
    )

    assert result.has_configured_candidate is True
    assert result.selected_chain == StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="medium",
        fallback=StageOverride(
            service="claude",
            model="haiku",
            effort="low",
        ),
    )


def test_select_configured_candidate_chain_rebuilds_a_compact_chain_for_available_configured_candidates() -> (
    None
):
    override = StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(
            service="opencode",
            model="deepseek-v4-flash",
            effort="high",
            fallback=StageOverride(
                service="claude",
                model="opus",
                effort="high",
            ),
        ),
    )

    result = select_configured_candidate_chain(
        override,
        configured_service_names=("codex", "claude"),
        available_service_names=("codex",),
    )

    assert result.has_configured_candidate is True
    assert result.selected_chain == StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(
            service="claude",
            model="opus",
            effort="high",
        ),
    )


def test_select_configured_candidate_chain_reports_when_no_candidate_is_configured() -> (
    None
):
    override = StageOverride(
        service="opencode",
        model="deepseek-v4-flash",
        effort="high",
        fallback=StageOverride(
            service="claude",
            model="haiku",
            effort="low",
        ),
    )

    result = select_configured_candidate_chain(
        override,
        configured_service_names=("codex",),
        available_service_names=("codex",),
    )

    assert result.has_configured_candidate is False
    assert result.selected_chain is None


def test_stage_override_chain_configured_candidate_availability_rebuilds_compact_chain() -> (
    None
):
    override = StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(
            service="missing",
            model="unused",
            effort="high",
            fallback=StageOverride(
                service="claude",
                model="opus",
                effort="high",
            ),
        ),
    )

    chain = StageOverrideChain(
        override=override,
        configured_service_names=("codex", "claude"),
    )

    result = chain.configured_candidate_availability({"codex": True, "claude": False})

    assert result.available_candidates == (override,)
    assert result.exhausted_candidates == (
        StageOverride(service="claude", model="opus", effort="high"),
    )
    assert result.selection.selected_chain == StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=StageOverride(
            service="claude",
            model="opus",
            effort="high",
        ),
    )


def test_stage_override_chain_configured_candidate_availability_returns_first_configured_chain_when_all_exhausted() -> (
    None
):
    override = StageOverride(
        service="missing",
        fallback=StageOverride(
            service="codex",
            model="gpt-5.4-mini",
            effort="medium",
            fallback=StageOverride(
                service="claude",
                model="haiku",
                effort="low",
            ),
        ),
    )

    chain = StageOverrideChain(
        override=override,
        configured_service_names=("codex", "claude"),
    )

    result = chain.configured_candidate_availability({"codex": False, "claude": False})

    assert result.has_available_candidate is False
    assert result.exhausted_candidates == chain.configured_candidates.candidates
    assert result.selection.selected_chain == StageOverride(
        service="codex",
        model="gpt-5.4-mini",
        effort="medium",
        fallback=StageOverride(
            service="claude",
            model="haiku",
            effort="low",
        ),
    )
