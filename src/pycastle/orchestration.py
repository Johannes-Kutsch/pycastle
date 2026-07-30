from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.iteration._deps import ImproveMode
    from pycastle.services.service_registry import ServiceRegistry


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    service_registry: ServiceRegistry,
    improve_mode: ImproveMode,
) -> None:
    from pycastle.iteration.orchestrator import run as run_orchestrator

    await run_orchestrator(
        env,
        repo_root,
        service_registry=service_registry,
        improve_mode=improve_mode,
    )
