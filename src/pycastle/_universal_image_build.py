from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol

from .errors import ConfigValidationError
from .services._docker_build_output import BuildOutcome

if TYPE_CHECKING:
    from .config import Config


_MISSING_DOCKER_IMAGE_NAME_MESSAGE = (
    "docker_image_name is not set. Run `pycastle init` to configure your project."
)
_DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"


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


def _normalize_python_version(version: str) -> str:
    stripped_version = version.strip()
    parts = stripped_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else stripped_version


def _resolve_python_version(project_root: Path) -> str | None:
    python_version_file = project_root / ".python-version"
    if not python_version_file.is_file():
        return None
    return _normalize_python_version(python_version_file.read_text())


def resolve_universal_dockerfile(
    pycastle_dir: Path | str,
    *,
    bundled_defaults_dir: Path | None = None,
) -> Path:
    if isinstance(pycastle_dir, str):
        pycastle_dir = Path(pycastle_dir)
    elif not isinstance(pycastle_dir, Path):
        raise TypeError(
            "resolve_universal_dockerfile() expects only a pycastle_dir Path"
        )
    if bundled_defaults_dir is None:
        bundled_defaults_dir = _DEFAULTS_DIR
    local_dockerfile = pycastle_dir / "Dockerfile"
    dockerfile_path = (
        local_dockerfile
        if local_dockerfile.is_file()
        else bundled_defaults_dir / "Dockerfile"
    )
    if not dockerfile_path.is_file():
        raise ConfigValidationError(
            "No bundled universal Dockerfile default exists",
            invalid_value=str(pycastle_dir),
        )
    return dockerfile_path


def resolve_universal_image_build_request(
    cfg: Config,
    *,
    project_root: Path,
    options: UniversalImageBuildOptions = UniversalImageBuildOptions(),
) -> UniversalImageBuildRequest:
    pycastle_dir = project_root / "pycastle"
    python_version = options.python_version
    if python_version is None:
        python_version = _resolve_python_version(project_root)
    return UniversalImageBuildRequest(
        image_tag=cfg.docker_image_name,
        dockerfile_path=resolve_universal_dockerfile(pycastle_dir),
        context_dir=project_root,
        options=UniversalImageBuildOptions(
            python_version=python_version,
            no_cache=options.no_cache,
            stream=options.stream,
            terse=options.terse,
        ),
    )


def build_universal_image(
    adapter: UniversalImageBuildAdapter,
    request: UniversalImageBuildRequest,
) -> BuildOutcome | None:
    if not request.image_tag:
        raise ConfigValidationError(_MISSING_DOCKER_IMAGE_NAME_MESSAGE)
    return adapter.build(request)
