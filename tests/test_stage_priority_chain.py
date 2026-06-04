from pycastle.config.types import StageOverride
from pycastle.service_availability import iter_stage_chain as iter_legacy_stage_chain
from pycastle.stage_priority_chain import iter_stage_chain


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
