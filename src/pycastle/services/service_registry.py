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

    def resolve(self, override: StageOverride, now: datetime) -> StageOverride:
        for node in iter_stage_chain(override):
            svc = self._services.get(node.service)
            if svc is None or svc.is_available(now=now):
                return node
        return override

    def has_available(self, now: datetime) -> bool:
        return any(svc.is_available(now=now) for svc in self._services.values())

    def next_wake_time(self, now: datetime) -> datetime | None:
        exhausted = [
            svc for svc in self._services.values() if not svc.is_available(now=now)
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
