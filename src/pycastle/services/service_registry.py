from __future__ import annotations

from datetime import datetime

from ..config.types import StageOverride
from .agent_service import AgentService


class ServiceRegistry:
    def __init__(self, services: dict[str, AgentService]) -> None:
        self._services = services

    @property
    def services(self) -> dict[str, AgentService]:
        return dict(self._services)

    def resolve(self, override: StageOverride, now: datetime) -> StageOverride:
        primary_name = override.service
        primary_svc = self._services.get(primary_name)
        if primary_svc is None or primary_svc.is_available(now=now):
            return override
        if override.fallback is not None:
            fallback_name = override.fallback.service
            fallback_svc = self._services.get(fallback_name)
            if fallback_svc is not None and fallback_svc.is_available(now=now):
                return override.fallback
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
        for svc in self._services.values():
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
