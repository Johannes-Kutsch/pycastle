from __future__ import annotations

from pycastle.services.runtime_services import AgentService


def render_service_summary_line(
    service_name: str,
    service: AgentService,
) -> str | None:
    if service_name == "codex":
        return "Codex auth: local auth available"
    if service_name == "opencode":
        return "OpenCode auth: API key configured"

    account_names = getattr(service, "account_names", None)
    if not callable(account_names):
        return None

    names = account_names()
    if not names:
        return None
    if len(names) == 1:
        return f"Claude accounts: {names[0]} (active)"
    parts = [f"{names[0]} (active)"] + [f"{name} (standby)" for name in names[1:]]
    return "Claude accounts: " + ", ".join(parts)
