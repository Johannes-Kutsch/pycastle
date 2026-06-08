from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pycastle import _time as _time_module
from pycastle.agents.runner import AgentRunner
from pycastle.config.types import StageOverride
from pycastle.services.flag_profiles import AgentToolPolicyGroup
from pycastle.services.service_registry import ServiceRegistry


ToolPolicy = AgentToolPolicyGroup


@dataclass(frozen=True)
class PromptRunRequest:
    prompt: str
    mount_path: Path
    override: StageOverride
    tool_policy: ToolPolicy = ToolPolicy.FULL
    name: str = "Runtime Agent"
    status_display: Any = None
    work_body: str = ""


async def run_prompt(
    *,
    runner: AgentRunner,
    service_registry: ServiceRegistry,
    request: PromptRunRequest,
) -> str:
    resolved_override = service_registry.resolve(
        request.override,
        _time_module.now_local(),
    )
    return await runner.run_prompt(
        name=request.name,
        prompt=request.prompt,
        mount_path=request.mount_path,
        model=resolved_override.model,
        effort=resolved_override.effort,
        service=resolved_override.service,
        tool_policy=request.tool_policy,
        status_display=request.status_display,
        work_body=request.work_body,
    )
