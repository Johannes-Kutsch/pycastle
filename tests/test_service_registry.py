from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock


from pycastle.config.types import StageOverride
from pycastle.services.agent_service import AgentService
from pycastle.services.service_registry import ServiceRegistry


def _now() -> datetime:
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_svc(available: bool, wake: datetime | None = None) -> MagicMock:
    svc = MagicMock(spec=AgentService)
    svc.is_available.return_value = available
    if wake is not None:
        svc.next_wake_time.return_value = wake
    return svc


# --- resolve ---


def test_resolve_returns_primary_when_primary_service_is_available() -> None:
    svc = _make_svc(available=True)
    registry = ServiceRegistry(services={"claude": svc})
    override = StageOverride(service="claude")

    result = registry.resolve(override, _now())

    assert result is override


def test_resolve_returns_fallback_when_primary_exhausted_and_fallback_available() -> (
    None
):
    primary = _make_svc(available=False)
    fallback_svc = _make_svc(available=True)
    registry = ServiceRegistry(
        services={"primary": primary, "fallback": fallback_svc},
    )
    fallback_override = StageOverride(service="fallback")
    override = StageOverride(service="primary", fallback=fallback_override)

    result = registry.resolve(override, _now())

    assert result is fallback_override


def test_resolve_returns_primary_when_both_exhausted() -> None:
    primary = _make_svc(available=False)
    fallback_svc = _make_svc(available=False)
    registry = ServiceRegistry(
        services={"primary": primary, "fallback": fallback_svc},
    )
    fallback_override = StageOverride(service="fallback")
    override = StageOverride(service="primary", fallback=fallback_override)

    result = registry.resolve(override, _now())

    assert result is override


def test_resolve_does_not_treat_empty_service_as_default() -> None:
    svc = _make_svc(available=True)
    registry = ServiceRegistry(services={"claude": svc})
    override = StageOverride(service="")

    result = registry.resolve(override, _now())

    assert result is override
    svc.is_available.assert_not_called()


def test_resolve_returns_primary_when_service_not_registered() -> None:
    fallback_svc = _make_svc(available=True)
    registry = ServiceRegistry(services={"claude": fallback_svc})
    fallback_override = StageOverride(service="claude")
    override = StageOverride(service="codex", fallback=fallback_override)

    result = registry.resolve(override, _now())

    assert result is fallback_override


def test_resolve_returns_primary_when_no_stage_candidate_is_registered() -> None:
    registry = ServiceRegistry(services={})
    override = StageOverride(service="codex", fallback=StageOverride(service="claude"))

    result = registry.resolve(override, _now())

    assert result is override


def test_resolve_uses_first_available_configured_candidate_in_deep_chain() -> None:
    secondary = _make_svc(available=False)
    tertiary = _make_svc(available=True)
    registry = ServiceRegistry(
        services={"secondary": secondary, "tertiary": tertiary},
    )
    tertiary_override = StageOverride(service="tertiary")
    override = StageOverride(
        service="primary",
        fallback=StageOverride(
            service="secondary",
            fallback=StageOverride(service="missing", fallback=tertiary_override),
        ),
    )

    result = registry.resolve(override, _now())

    assert result.service == "tertiary"
    assert result.fallback is None


def test_resolve_returns_first_configured_candidate_when_all_configured_are_exhausted() -> (
    None
):
    fallback_svc = _make_svc(available=False)
    registry = ServiceRegistry(services={"fallback": fallback_svc})
    override = StageOverride(
        service="primary",
        fallback=StageOverride(service="fallback"),
    )

    result = registry.resolve(override, _now())

    assert result.service == "fallback"
    assert result.fallback is None


# --- has_available ---


def test_has_available_returns_true_when_any_service_available() -> None:
    exhausted = _make_svc(available=False)
    available = _make_svc(available=True)
    registry = ServiceRegistry(
        services={"exhausted": exhausted, "available": available},
    )

    assert registry.has_available(_now()) is True


def test_has_available_returns_false_when_all_exhausted() -> None:
    svc = _make_svc(available=False)
    registry = ServiceRegistry(services={"claude": svc})

    assert registry.has_available(_now()) is False


def test_has_available_returns_false_on_empty_registry() -> None:
    registry = ServiceRegistry(services={})

    assert registry.has_available(_now()) is False


# --- next_wake_time ---


def test_next_wake_time_returns_earliest_exhausted_wake_time() -> None:
    earlier = datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    later = datetime(2025, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    svc_a = _make_svc(available=False, wake=earlier)
    svc_b = _make_svc(available=False, wake=later)
    registry = ServiceRegistry(services={"a": svc_a, "b": svc_b})

    assert registry.next_wake_time(_now()) == earlier


def test_next_wake_time_returns_none_when_all_available() -> None:
    svc = _make_svc(available=True)
    registry = ServiceRegistry(services={"claude": svc})

    assert registry.next_wake_time(_now()) is None


def test_next_wake_time_returns_none_on_empty_registry() -> None:
    registry = ServiceRegistry(services={})

    assert registry.next_wake_time(_now()) is None


def test_next_wake_time_skips_available_services() -> None:
    wake = datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    exhausted = _make_svc(available=False, wake=wake)
    available = _make_svc(available=True)
    registry = ServiceRegistry(
        services={"exhausted": exhausted, "available": available},
    )

    assert registry.next_wake_time(_now()) == wake


# --- summary_lines ---


def _make_svc_with_accounts(names: list[str]) -> MagicMock:
    svc = MagicMock()
    svc.account_names.return_value = names
    return svc


def test_summary_lines_single_account() -> None:
    svc = _make_svc_with_accounts(["primary"])
    registry = ServiceRegistry(services={"claude": svc})

    assert registry.summary_lines() == ["Claude accounts: primary (active)"]


def test_summary_lines_multiple_accounts() -> None:
    svc = _make_svc_with_accounts(["primary", "secondary"])
    registry = ServiceRegistry(services={"claude": svc})

    assert registry.summary_lines() == [
        "Claude accounts: primary (active), secondary (standby)"
    ]


def test_summary_lines_prints_codex_auth_message() -> None:
    svc = MagicMock(spec=AgentService)
    registry = ServiceRegistry(services={"codex": svc})

    assert registry.summary_lines() == ["Codex auth: local auth available"]


def test_summary_lines_skips_services_with_empty_account_names() -> None:
    svc = _make_svc_with_accounts([])
    registry = ServiceRegistry(services={"claude": svc})

    assert registry.summary_lines() == []


# --- lookup by name ---


def test_lookup_known_key_returns_service() -> None:
    svc = _make_svc(available=True)
    registry = ServiceRegistry(services={"claude": svc})

    assert registry["claude"] is svc


def test_lookup_empty_string_returns_none() -> None:
    svc = _make_svc(available=True)
    registry = ServiceRegistry(services={"claude": svc})

    assert registry[""] is None


def test_lookup_unknown_key_returns_none() -> None:
    registry = ServiceRegistry(services={})

    assert registry["codex"] is None
