from __future__ import annotations

from datetime import datetime

from ..config.types import StageOverride
from ..service_availability import iter_stage_chain
from .agent_service import AgentService


class ServiceRegistry:
    def __init__(self, services: dict[str, AgentService]) -> None:
        self._services = services

    @property
    def services(self) -> dict[str, AgentService]:
        return dict(self._services)

    def _configured_nodes(self, override: StageOverride) -> list[StageOverride]:
        return [
            node
            for node in iter_stage_chain(override)
            if node.service in self._services
        ]

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
        return bool(self._configured_nodes(override))

    def resolve(self, override: StageOverride, now: datetime) -> StageOverride:
        configured = self._configured_nodes(override)
        for index, node in enumerate(configured):
            svc = self._services[node.service]
            if svc.is_available(now=now):
                if self._remaining_chain_is_fully_configured(node):
                    return node
                return self._build_chain(configured[index:])
        if configured:
            if self._remaining_chain_is_fully_configured(configured[0]):
                return configured[0]
            return self._build_chain(configured)
        return override

    def has_available(self, now: datetime) -> bool:
        return any(svc.is_available(now=now) for svc in self._services.values())

    def has_available_for(self, override: StageOverride, now: datetime) -> bool:
        return any(
            self._services[node.service].is_available(now=now)
            for node in self._configured_nodes(override)
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
        exhausted = [
            self._services[node.service]
            for node in self._configured_nodes(override)
            if not self._services[node.service].is_available(now=now)
        ]
        if not exhausted:
            return None
        return min(svc.next_wake_time() for svc in exhausted)

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
