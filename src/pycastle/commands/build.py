from __future__ import annotations

from pathlib import Path

from .._universal_image_build import (
    UniversalImageBuildOptions,
    UniversalImageBuildRequest,
    build_universal_image,
)
from ..config import Config, load_config, resolve_dockerfile
from ..services import DockerService
from ..services.docker_service import BuildOutcome


def main(
    no_cache: bool = False,
    stream: bool = False,
    terse: bool = False,
    docker_service: DockerService | None = None,
    cfg: Config | None = None,
) -> None:
    if cfg is None:
        cfg = load_config()

    if docker_service is None:
        docker_service = DockerService()

    python_version: str | None = None
    python_version_file = Path(".python-version")
    if python_version_file.exists():
        version = python_version_file.read_text().strip()
        parts = version.split(".")
        python_version = ".".join(parts[:2]) if len(parts) >= 2 else version

    print(f"Building {cfg.docker_image_name}...")
    outcome = build_universal_image(
        docker_service,
        UniversalImageBuildRequest(
            image_tag=cfg.docker_image_name,
            dockerfile_path=resolve_dockerfile(Path("pycastle")),
            context_dir=Path("."),
            options=UniversalImageBuildOptions(
                no_cache=no_cache,
                stream=stream,
                terse=terse,
                python_version=python_version,
            ),
        ),
    )

    if not stream:
        print("Build complete.")
    elif outcome == BuildOutcome.FULL_CACHE_HIT and not terse:
        print("Image up to date.")
