from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .errors import ConfigValidationError
from .services._docker_build_output import BuildOutcome


_MISSING_DOCKER_IMAGE_NAME_MESSAGE = (
    "docker_image_name is not set. Run `pycastle init` to configure your project."
)


@dataclass(frozen=True)
class UniversalImageBuildOptions:
    python_version: str | None = None
    no_cache: bool = False
    stream: bool = False
    terse: bool = False


@dataclass(frozen=True)
class UniversalImageBuildRequest:
    image_tag: str
    dockerfile_path: Path
    context_dir: Path
    options: UniversalImageBuildOptions = field(
        default_factory=UniversalImageBuildOptions
    )


class UniversalImageBuildAdapter(Protocol):
    def build(self, request: UniversalImageBuildRequest) -> BuildOutcome | None: ...


def build_universal_image(
    adapter: UniversalImageBuildAdapter,
    request: UniversalImageBuildRequest,
) -> BuildOutcome | None:
    if not request.image_tag:
        raise ConfigValidationError(_MISSING_DOCKER_IMAGE_NAME_MESSAGE)
    return adapter.build(request)
