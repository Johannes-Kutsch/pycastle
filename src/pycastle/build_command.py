from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .config import Config, load_config
from .errors import ConfigValidationError
from .services import DockerService
from .services.docker_service import BuildOutcome


def main(
    no_cache: bool = False,
    stream: bool = False,
    docker_service: DockerService | None = None,
    cfg: Config | None = None,
    on_rebuild_start: Callable[[], None] | None = None,
) -> None:
    if cfg is None:
        cfg = load_config()
    if not cfg.docker_image_name:
        raise ConfigValidationError(
            "docker_image_name is not set. Run `pycastle init` to configure your project."
        )

    if docker_service is None:
        docker_service = DockerService()

    python_version: str | None = None
    python_version_file = Path(".python-version")
    if python_version_file.exists():
        version = python_version_file.read_text().strip()
        parts = version.split(".")
        python_version = ".".join(parts[:2]) if len(parts) >= 2 else version

    outcome = docker_service.build_image(
        cfg.docker_image_name,
        cfg.dockerfile,
        Path("."),
        no_cache=no_cache,
        stream=stream,
        python_version=python_version,
        on_rebuild_start=on_rebuild_start,
    )

    if not stream:
        print("Build complete.")
    elif outcome == BuildOutcome.FULL_CACHE_HIT:
        print("Image up to date.")
