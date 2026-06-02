from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config.types import StageOverride
from ..service_availability import iter_stage_chain
from .agent_service import AgentService


@dataclass(frozen=True)
class _ConfiguredCandidate:
    override: StageOverride
    service: AgentService
    is_available: bool


@dataclass(frozen=True)
class _StageAvailability:
    candidates: tuple[_ConfiguredCandidate, ...]

    def has_configured_candidate(self) -> bool:
        return bool(self.candidates)

    def first_available_index(self) -> int | None:
        for index, candidate in enumerate(self.candidates):
            if candidate.is_available:
                return index
        return None

    def next_wake_time(self) -> datetime | None:
        exhausted = [
            candidate.service
            for candidate in self.candidates
            if not candidate.is_available
        ]
        if not exhausted:
            return None
        return min(service.next_wake_time() for service in exhausted)


class ServiceRegistry:
    def __init__(self, services: dict[str, AgentService]) -> None:
        self._services = services

    @property
    def services(self) -> dict[str, AgentService]:
        return dict(self._services)

    def _configured_candidates(
        self, override: StageOverride
    ) -> tuple[StageOverride, ...]:
        return tuple(
            node
            for node in iter_stage_chain(override)
            if node.service in self._services
        )

    def _stage_availability(
        self, override: StageOverride, now: datetime
    ) -> _StageAvailability:
        candidates: list[_ConfiguredCandidate] = []
        for node in self._configured_candidates(override):
            service = self._services[node.service]
            candidates.append(
                _ConfiguredCandidate(
                    override=node,
                    service=service,
                    is_available=service.is_available(now=now),
                )
            )
        return _StageAvailability(tuple(candidates))

    def _remaining_chain_is_fully_configured(self, override: StageOverride) -> bool:
        return all(
            node.service in self._services for node in iter_stage_chain(override)
        )

    def _build_chain(self, nodes: list[StageOverride]) -> StageOverride:
        chain: StageOverride | None = None
        for node in reversed(nodes):
            chain = StageOverride(
                service=node.service,
                model=node.model,
                effort=node.effort,
                fallback=chain,
            )
        if chain is None:
            raise RuntimeError("Cannot build stage chain from empty node list")
        return chain

    def has_configured_candidate(self, override: StageOverride) -> bool:
        return bool(self._configured_candidates(override))

    def resolve(self, override: StageOverride, now: datetime) -> StageOverride:
        stage_availability = self._stage_availability(override, now)
        available_index = stage_availability.first_available_index()
        if available_index is not None:
            available_override = stage_availability.candidates[available_index].override
            if self._remaining_chain_is_fully_configured(available_override):
                return available_override
            return self._build_chain(
                [
                    candidate.override
                    for candidate in stage_availability.candidates[available_index:]
                ]
            )
        if stage_availability.has_configured_candidate():
            first_configured = stage_availability.candidates[0].override
            if self._remaining_chain_is_fully_configured(first_configured):
                return first_configured
            return self._build_chain(
                [candidate.override for candidate in stage_availability.candidates]
            )
        return override

    def has_available(self, now: datetime) -> bool:
        return any(svc.is_available(now=now) for svc in self._services.values())

    def has_available_for(self, override: StageOverride, now: datetime) -> bool:
        return (
            self._stage_availability(override, now).first_available_index() is not None
        )

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
        return self._stage_availability(override, now).next_wake_time()

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
