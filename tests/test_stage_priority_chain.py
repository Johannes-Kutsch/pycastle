from pycastle.config.types import StageOverride
from pycastle.stage_priority_chain import StageOverrideChain


def test_stage_override_chain_selects_later_configured_priority_chain() -> None:
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

    result = StageOverrideChain(
        override=override,
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


def test_stage_override_chain_retains_compact_fallback_for_available_configured_candidates() -> (
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


def test_stage_override_chain_reports_exhausted_configured_candidates() -> None:
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


def test_stage_override_chain_renders_missing_service_names_with_coherent_labels() -> (
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

    assert [
        (label, entry.service)
        for label, entry in zip(result.validation_labels, result.entries, strict=True)
    ] == [
        ("plan", ""),
        ("plan fallback", "claude"),
    ]
    assert result.rendered_chain_label == "<missing> -> claude"
    assert result.has_configured_candidate is False


def test_stage_override_chain_deduplicates_referenced_service_names() -> None:
    override = StageOverride(
        service="codex",
        fallback=StageOverride(
            service="claude",
            fallback=StageOverride(
                service="codex",
                fallback=StageOverride(
                    service="",
                    fallback=StageOverride(service="opencode"),
                ),
            ),
        ),
    )

    result = StageOverrideChain(override=override)

    assert result.referenced_service_names == ("codex", "claude", "opencode")
