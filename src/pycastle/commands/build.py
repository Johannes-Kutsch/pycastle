from __future__ import annotations

from pathlib import Path

from .._universal_image_build import (
    UniversalImageBuildAdapter,
    UniversalImageBuildOptions,
    build_universal_image,
    resolve_universal_image_build_request,
)
from ..config import Config, load_config
from ..services import DockerService


def main(
    no_cache: bool = False,
    stream: bool = False,
    terse: bool = False,
    docker_service: UniversalImageBuildAdapter | None = None,
    cfg: Config | None = None,
) -> None:
    if cfg is None:
        cfg = load_config()

    if docker_service is None:
        docker_service = DockerService()

    build_universal_image(
        docker_service,
        resolve_universal_image_build_request(
            cfg,
            project_root=Path("."),
            options=UniversalImageBuildOptions(
                no_cache=no_cache,
                stream=stream,
                terse=terse,
            ),
        ),
    )
