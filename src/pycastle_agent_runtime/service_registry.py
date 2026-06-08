from __future__ import annotations

from datetime import datetime

from pycastle.config.types import StageOverride
from pycastle.services.agent_service import AgentService

from .stage_priority_chain import (
    configured_candidate_chain,
    select_configured_candidate_chain,
)


class ServiceRegistry:
    def __init__(self, services: dict[str, AgentService]) -> None:
        self._services = services

    @property
    def services(self) -> dict[str, AgentService]:
        return dict(self._services)

    def _configured_candidate_overrides(
        self, override: StageOverride
    ) -> tuple[StageOverride, ...]:
        return configured_candidate_chain(
            override, configured_service_names=tuple(self._services)
        ).candidates

    def _availability_by_service(
        self, overrides: tuple[StageOverride, ...], now: datetime
    ) -> dict[str, bool]:
        availability: dict[str, bool] = {}
        for node in overrides:
            if node.service in availability:
                continue
            availability[node.service] = self._services[node.service].is_available(
                now=now
            )
        return availability

    def _exhausted_services_for(
        self, override: StageOverride, now: datetime
    ) -> tuple[AgentService, ...]:
        configured_overrides = self._configured_candidate_overrides(override)
        availability = self._availability_by_service(configured_overrides, now)
        return tuple(
            self._services[node.service]
            for node in configured_overrides
            if not availability[node.service]
        )

    def has_configured_candidate(self, override: StageOverride) -> bool:
        return configured_candidate_chain(
            override, configured_service_names=tuple(self._services)
        ).has_configured_candidate

    def resolve(self, override: StageOverride, now: datetime) -> StageOverride:
        configured_overrides = self._configured_candidate_overrides(override)
        availability = self._availability_by_service(configured_overrides, now)
        selection = select_configured_candidate_chain(
            override,
            configured_service_names=tuple(
                node.service for node in configured_overrides
            ),
            available_service_names=tuple(
                node.service
                for node in configured_overrides
                if availability[node.service]
            ),
        )
        return selection.selected_chain or override

    def has_available(self, now: datetime) -> bool:
        return any(svc.is_available(now=now) for svc in self._services.values())

    def has_available_for(self, override: StageOverride, now: datetime) -> bool:
        configured_overrides = self._configured_candidate_overrides(override)
        availability = self._availability_by_service(configured_overrides, now)
        return any(availability[node.service] for node in configured_overrides)

    def next_wake_time(self, now: datetime) -> datetime | None:
        exhausted = [
            svc for svc in self._services.values() if not svc.is_available(now=now)
        ]
        if not exhausted:
            return None
        return min(svc.next_wake_time() for svc in exhausted)

    def next_wake_time_for(
        self, override: StageOverride, now: datetime
    ) -> datetime | None:
        exhausted = self._exhausted_services_for(override, now)
        if not exhausted:
            return None
        return min(service.next_wake_time() for service in exhausted)

    def __getitem__(self, key: str) -> AgentService | None:
        return self._services.get(key)

    def summary_lines(self) -> list[str]:
        lines = []
        for name, svc in self._services.items():
            if name == "codex":
                lines.append("Codex auth: local auth available")
                continue
            if name == "opencode":
                lines.append("OpenCode auth: API key configured")
                continue
            if not hasattr(svc, "account_names"):
                continue
            names: list[str] = svc.account_names()  # type: ignore[attr-defined]
            if not names:
                continue
            if len(names) == 1:
                lines.append(f"Claude accounts: {names[0]} (active)")
            else:
                parts = [f"{names[0]} (active)"] + [f"{n} (standby)" for n in names[1:]]
                lines.append("Claude accounts: " + ", ".join(parts))
        return lines
