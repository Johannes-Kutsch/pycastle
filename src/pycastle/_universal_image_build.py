from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .services._docker_build_output import BuildOutcome


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
