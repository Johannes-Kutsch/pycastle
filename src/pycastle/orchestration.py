from __future__ import annotations

from pathlib import Path

from .services.service_registry import ServiceRegistry

from .iteration._deps import ImproveMode


async def run(
    env: dict[str, str],
    repo_root: Path,
    *,
    service_registry: ServiceRegistry,
    improve_mode: ImproveMode,
) -> None:
    from .iteration.orchestrator import run as run_orchestrator

    await run_orchestrator(
        env,
        repo_root,
        service_registry=service_registry,
        improve_mode=improve_mode,
    )
