from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from .agent_service import AgentService
from ..config.types import StageOverride
from ..stage_priority_chain import (
    StageOverrideChain,
)

ServiceSummaryRenderer = Callable[[str, AgentService], str | None]


class ServiceRegistry:
    def __init__(self, services: Mapping[str, AgentService]) -> None:
        self._services = dict(services)

    @property
    def services(self) -> dict[str, AgentService]:
        return dict(self._services)

    def _configured_candidates_chain(
        self, override: StageOverride
    ) -> StageOverrideChain:
        return StageOverrideChain(
            override=override,
            configured_service_names=tuple(self._services),
        )

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

    def has_configured_candidate(self, override: StageOverride) -> bool:
        return self._configured_candidates_chain(
            override,
        ).configured_candidates.has_configured_candidate

    def resolve(self, override: StageOverride, now: datetime) -> StageOverride:
        configured_candidates = self._configured_candidates_chain(
            override
        ).configured_candidates.candidates
        availability = self._availability_by_service(configured_candidates, now)
        selection = StageOverrideChain(
            available_service_names=tuple(
                node.service
                for node in configured_candidates
                if availability[node.service]
            ),
            override=override,
            configured_service_names=tuple(self._services),
        ).configured_candidate_selection
        return selection.selected_chain or override

    def has_available(self, now: datetime) -> bool:
        return any(svc.is_available(now=now) for svc in self._services.values())

    def has_available_for(self, override: StageOverride, now: datetime) -> bool:
        configured_overrides = self._configured_candidates_chain(
            override
        ).configured_candidates.candidates
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
        configured_overrides = self._configured_candidates_chain(
            override
        ).configured_candidates.candidates
        availability = self._availability_by_service(configured_overrides, now)
        exhausted = [
            self._services[node.service]
            for node in configured_overrides
            if not availability[node.service]
        ]
        if not exhausted:
            return None
        return min(service.next_wake_time() for service in exhausted)

    def __getitem__(self, key: str) -> AgentService | None:
        return self._services.get(key)

    def summary_lines(
        self,
        render_summary_line: ServiceSummaryRenderer,
    ) -> list[str]:
        lines = []
        for name, svc in self._services.items():
            line = render_summary_line(name, svc)
            if line is None:
                continue
            lines.append(line)
        return lines


__all__ = ["ServiceRegistry", "ServiceSummaryRenderer"]
