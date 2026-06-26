from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from pycastle.config.types import StageOverride
from pycastle.services.service_registry import ServiceRegistry
from pycastle.services.runtime_services import AgentService
from pycastle.services.runtime_services import OpenCodeService

runtime = SimpleNamespace(ServiceRegistry=ServiceRegistry, StageOverride=StageOverride)


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
    registry = runtime.ServiceRegistry(services={"claude": svc})
    override = runtime.StageOverride(service="claude")

    result = registry.resolve(override, _now())

    assert result is override


def test_resolve_returns_fallback_when_primary_exhausted_and_fallback_available() -> (
    None
):
    primary = _make_svc(available=False)
    fallback_svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(
        services={"primary": primary, "fallback": fallback_svc},
    )
    fallback_override = runtime.StageOverride(service="fallback")
    override = runtime.StageOverride(service="primary", fallback=fallback_override)

    result = registry.resolve(override, _now())

    assert result is fallback_override


def test_resolve_returns_primary_when_both_exhausted() -> None:
    primary = _make_svc(available=False)
    fallback_svc = _make_svc(available=False)
    registry = runtime.ServiceRegistry(
        services={"primary": primary, "fallback": fallback_svc},
    )
    fallback_override = runtime.StageOverride(service="fallback")
    override = runtime.StageOverride(service="primary", fallback=fallback_override)

    result = registry.resolve(override, _now())

    assert result is override


def test_resolve_does_not_treat_empty_service_as_default() -> None:
    svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"claude": svc})
    override = runtime.StageOverride(service="")

    result = registry.resolve(override, _now())

    assert result is override
    svc.is_available.assert_not_called()


def test_resolve_returns_primary_when_service_not_registered() -> None:
    fallback_svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"claude": fallback_svc})
    fallback_override = runtime.StageOverride(service="claude")
    override = runtime.StageOverride(service="codex", fallback=fallback_override)

    result = registry.resolve(override, _now())

    assert result is fallback_override


def test_resolve_returns_primary_when_no_stage_candidate_is_registered() -> None:
    registry = runtime.ServiceRegistry(services={})
    override = runtime.StageOverride(
        service="codex", fallback=runtime.StageOverride(service="claude")
    )

    result = registry.resolve(override, _now())

    assert result is override


def test_resolve_uses_first_available_configured_candidate_in_deep_chain() -> None:
    secondary = _make_svc(available=False)
    tertiary = _make_svc(available=True)
    registry = runtime.ServiceRegistry(
        services={"secondary": secondary, "tertiary": tertiary},
    )
    tertiary_override = runtime.StageOverride(service="tertiary")
    override = runtime.StageOverride(
        service="primary",
        fallback=runtime.StageOverride(
            service="secondary",
            fallback=runtime.StageOverride(
                service="missing", fallback=tertiary_override
            ),
        ),
    )

    result = registry.resolve(override, _now())

    assert result.service == "tertiary"
    assert result.fallback is None


def test_resolve_rebuilds_compact_priority_chain_with_retained_model_and_effort() -> (
    None
):
    codex = _make_svc(available=True)
    claude = _make_svc(available=False)
    registry = runtime.ServiceRegistry(services={"codex": codex, "claude": claude})
    override = runtime.StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=runtime.StageOverride(
            service="missing",
            model="unused-model",
            effort="high",
            fallback=runtime.StageOverride(
                service="claude",
                model="opus",
                effort="high",
            ),
        ),
    )

    result = registry.resolve(override, _now())

    assert result == runtime.StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
        fallback=runtime.StageOverride(
            service="claude",
            model="opus",
            effort="high",
        ),
    )


def test_resolve_returns_first_configured_candidate_when_all_configured_are_exhausted() -> (
    None
):
    fallback_svc = _make_svc(available=False)
    registry = runtime.ServiceRegistry(services={"fallback": fallback_svc})
    override = runtime.StageOverride(
        service="primary",
        fallback=runtime.StageOverride(service="fallback"),
    )

    result = registry.resolve(override, _now())

    assert result.service == "fallback"
    assert result.fallback is None


def test_resolve_falls_through_exhausted_opencode_before_sleep() -> None:
    opencode = OpenCodeService(api_key="go-key")
    opencode.mark_exhausted(datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc))
    fallback_svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(
        services={"opencode": opencode, "claude": fallback_svc},
    )
    override = runtime.StageOverride(
        service="opencode",
        model="kimi-k2.6",
        effort="medium",
        fallback=runtime.StageOverride(
            service="claude", model="sonnet", effort="medium"
        ),
    )

    result = registry.resolve(override, _now())

    assert result.service == "claude"
    assert result.fallback is None


def test_resolve_skips_exhausted_opencode_slots_before_fallback() -> None:
    opencode = OpenCodeService(
        accounts=[
            ("account 1", "key-1"),
            ("account 2", "key-2"),
        ]
    )
    opencode.mark_exhausted(
        datetime(2040, 1, 1, 13, 0, 0, tzinfo=timezone.utc),
        _now=_now(),
    )
    _ = opencode.build_env()
    opencode.mark_exhausted(
        datetime(2040, 1, 1, 13, 0, 0, tzinfo=timezone.utc),
        _now=_now(),
    )
    fallback_svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(
        services={"opencode": opencode, "claude": fallback_svc},
    )
    override = runtime.StageOverride(
        service="opencode",
        model="kimi-k2.6",
        effort="medium",
        fallback=runtime.StageOverride(
            service="claude", model="sonnet", effort="medium"
        ),
    )

    result = registry.resolve(override, _now())

    assert result.service == "claude"
    assert result.fallback is None


# --- has_available_for ---


def test_has_available_for_ignores_unconfigured_stage_candidates() -> None:
    claude = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"claude": claude})
    override = runtime.StageOverride(
        service="missing-primary",
        fallback=runtime.StageOverride(service="claude"),
    )

    assert registry.has_available_for(override, _now()) is True


def test_has_available_for_returns_false_when_all_configured_candidates_exhausted() -> (
    None
):
    codex = _make_svc(available=False)
    claude = _make_svc(available=False)
    registry = runtime.ServiceRegistry(services={"codex": codex, "claude": claude})
    override = runtime.StageOverride(
        service="missing-primary",
        fallback=runtime.StageOverride(
            service="codex",
            fallback=runtime.StageOverride(service="claude"),
        ),
    )

    assert registry.has_available_for(override, _now()) is False


def test_has_configured_candidate_ignores_unconfigured_priority_chain_nodes() -> None:
    claude = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"claude": claude})
    override = runtime.StageOverride(
        service="missing-primary",
        fallback=runtime.StageOverride(service="claude"),
    )

    assert registry.has_configured_candidate(override) is True


# --- has_available ---


def test_has_available_returns_true_when_any_service_available() -> None:
    exhausted = _make_svc(available=False)
    available = _make_svc(available=True)
    registry = runtime.ServiceRegistry(
        services={"exhausted": exhausted, "available": available},
    )

    assert registry.has_available(_now()) is True


def test_has_available_returns_false_when_all_exhausted() -> None:
    svc = _make_svc(available=False)
    registry = runtime.ServiceRegistry(services={"claude": svc})

    assert registry.has_available(_now()) is False


def test_has_available_returns_false_on_empty_registry() -> None:
    registry = runtime.ServiceRegistry(services={})

    assert registry.has_available(_now()) is False


# --- next_wake_time ---


def test_next_wake_time_returns_earliest_exhausted_wake_time() -> None:
    earlier = datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    later = datetime(2025, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    svc_a = _make_svc(available=False, wake=earlier)
    svc_b = _make_svc(available=False, wake=later)
    registry = runtime.ServiceRegistry(services={"a": svc_a, "b": svc_b})

    assert registry.next_wake_time(_now()) == earlier


def test_next_wake_time_returns_none_when_all_available() -> None:
    svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"claude": svc})

    assert registry.next_wake_time(_now()) is None


def test_next_wake_time_returns_none_on_empty_registry() -> None:
    registry = runtime.ServiceRegistry(services={})

    assert registry.next_wake_time(_now()) is None


def test_next_wake_time_skips_available_services() -> None:
    wake = datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    exhausted = _make_svc(available=False, wake=wake)
    available = _make_svc(available=True)
    registry = runtime.ServiceRegistry(
        services={"exhausted": exhausted, "available": available},
    )

    assert registry.next_wake_time(_now()) == wake


def test_next_wake_time_for_includes_configured_exhausted_opencode_only() -> None:
    opencode = OpenCodeService(api_key="go-key")
    opencode.mark_exhausted(datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc))
    registry = runtime.ServiceRegistry(services={"opencode": opencode})
    override = runtime.StageOverride(
        service="missing",
        fallback=runtime.StageOverride(
            service="opencode",
            model="kimi-k2.6",
            effort="medium",
        ),
    )

    assert registry.next_wake_time_for(override, _now()) == datetime(
        2025, 1, 1, 13, 2, 0, tzinfo=timezone.utc
    )


def test_next_wake_time_for_skips_available_configured_fallbacks() -> None:
    exhausted_wake = datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc)
    codex = _make_svc(available=False, wake=exhausted_wake)
    claude = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"codex": codex, "claude": claude})
    override = runtime.StageOverride(
        service="missing-primary",
        fallback=runtime.StageOverride(
            service="codex",
            fallback=runtime.StageOverride(service="claude"),
        ),
    )

    assert registry.next_wake_time_for(override, _now()) == exhausted_wake


def test_next_wake_time_for_returns_none_when_priority_chain_has_no_configured_candidate() -> (
    None
):
    registry = runtime.ServiceRegistry(services={"claude": _make_svc(available=True)})
    override = runtime.StageOverride(
        service="missing-primary",
        fallback=runtime.StageOverride(service="missing-fallback"),
    )

    assert registry.next_wake_time_for(override, _now()) is None


# --- summary_lines ---


def _make_svc_with_accounts(names: list[str]) -> MagicMock:
    svc = MagicMock()
    svc.account_names.return_value = names
    return svc


def test_summary_lines_single_account() -> None:
    svc = _make_svc_with_accounts(["primary"])
    registry = runtime.ServiceRegistry(services={"claude": svc})

    assert registry.summary_lines(
        lambda name, service: (
            f"{name}:{','.join(service.account_names())}"  # type: ignore[attr-defined]
        )
    ) == ["claude:primary"]


def test_summary_lines_multiple_accounts() -> None:
    svc = _make_svc_with_accounts(["primary", "secondary"])
    registry = runtime.ServiceRegistry(services={"claude": svc})

    assert registry.summary_lines(
        lambda name, service: (
            f"{name}:{','.join(service.account_names())}"  # type: ignore[attr-defined]
        )
    ) == ["claude:primary,secondary"]


def test_summary_lines_prints_codex_auth_message() -> None:
    svc = MagicMock(spec=AgentService)
    registry = runtime.ServiceRegistry(services={"codex": svc})

    assert registry.summary_lines(lambda name, _service: f"configured:{name}") == [
        "configured:codex"
    ]


def test_summary_lines_prints_opencode_auth_message_when_configured() -> None:
    registry = runtime.ServiceRegistry(
        services={"opencode": OpenCodeService(api_key="go-key")}
    )

    assert registry.summary_lines(lambda name, _service: f"configured:{name}") == [
        "configured:opencode"
    ]


def test_summary_lines_skips_services_with_empty_account_names() -> None:
    svc = _make_svc_with_accounts([])
    registry = runtime.ServiceRegistry(services={"claude": svc})

    assert registry.summary_lines(lambda _name, _service: None) == []


# --- lookup by name ---


def test_lookup_known_key_returns_service() -> None:
    svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"claude": svc})

    assert registry["claude"] is svc


def test_lookup_empty_string_returns_none() -> None:
    svc = _make_svc(available=True)
    registry = runtime.ServiceRegistry(services={"claude": svc})

    assert registry[""] is None


def test_lookup_unknown_key_returns_none() -> None:
    registry = runtime.ServiceRegistry(services={})

    assert registry["codex"] is None
