from __future__ import annotations

import sys
from pathlib import Path

from .config import DOCKERFILE, DOCKER_IMAGE
from .docker_service import DockerService
from .errors import DockerServiceError


def main(no_cache: bool = False, docker_service: DockerService | None = None) -> None:
    if docker_service is None:
        docker_service = DockerService()

    python_version: str | None = None
    python_version_file = Path(".python-version")
    if python_version_file.exists():
        version = python_version_file.read_text().strip()
        parts = version.split(".")
        python_version = ".".join(parts[:2]) if len(parts) >= 2 else version

    try:
        docker_service.build_image(
            DOCKER_IMAGE,
            DOCKERFILE,
            Path("."),
            no_cache=no_cache,
            python_version=python_version,
        )
    except DockerServiceError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    sys.exit(0)
