from __future__ import annotations

from pathlib import Path

from .config import Config, load_config
from .errors import ConfigValidationError
from .services import DockerService


def main(
    no_cache: bool = False,
    docker_service: DockerService | None = None,
    cfg: Config | None = None,
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

    docker_service.build_image(
        cfg.docker_image_name,
        cfg.dockerfile,
        Path("."),
        no_cache=no_cache,
        python_version=python_version,
    )

    print("Build complete.")
