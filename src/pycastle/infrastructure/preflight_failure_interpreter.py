from __future__ import annotations

import dataclasses
import re
import shlex
import tomllib
from pathlib import Path
from typing import Sequence

from ..errors import SetupPhaseError

_DECLARED_TOOL_MISSING_PATTERNS = (
    re.compile(r"\b(?P<tool>[A-Za-z0-9_.-]+): command not found\b", re.IGNORECASE),
    re.compile(r"\b(?P<tool>[A-Za-z0-9_.-]+): not found\b", re.IGNORECASE),
    re.compile(
        r"\bNo module named ['\"]?(?P<tool>[A-Za-z0-9_.-]+)['\"]?\b", re.IGNORECASE
    ),
)
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PYTHON_MODULE_LAUNCHERS = {"py"}


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str | None:
    match = _REQUIREMENT_NAME_RE.match(requirement)
    if match is None:
        return None
    return _normalize_package_name(match.group(1))


def _configured_tool_name(command: str, check_name: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if len(parts) >= 3:
        launcher = _normalize_package_name(Path(parts[0]).name)
        if launcher in _PYTHON_MODULE_LAUNCHERS or launcher.startswith("python"):
            try:
                module_flag_index = parts.index("-m", 1)
            except ValueError:
                module_flag_index = -1
            if module_flag_index >= 1 and module_flag_index + 1 < len(parts):
                return _normalize_package_name(parts[module_flag_index + 1])
    if parts:
        return _normalize_package_name(Path(parts[0]).name)
    return _normalize_package_name(check_name)


@dataclasses.dataclass(frozen=True)
class PythonDependencyMetadata:
    declared_packages: frozenset[str]
    source: str = ""
    package_sources: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class PreflightCommandFailure:
    check_name: str
    command: str
    output: str


@dataclasses.dataclass(frozen=True)
class OrdinaryCheckFailure:
    tool: str
    failure: PreflightCommandFailure


@dataclasses.dataclass(frozen=True)
class MissingDeclaredTool:
    tool: str
    dependency_source: str


_PreflightToolFailureClassification = OrdinaryCheckFailure | MissingDeclaredTool


def load_python_dependency_metadata(project_root: Path) -> PythonDependencyMetadata:
    package_sources: dict[str, str] = {}
    pyproject_path = project_root / "pyproject.toml"
    if pyproject_path.exists():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project")
        if isinstance(project, dict):
            requirements: list[str] = []
            dependencies = project.get("dependencies")
            if isinstance(dependencies, list):
                requirements.extend(req for req in dependencies if isinstance(req, str))

            optional_dependencies = project.get("optional-dependencies")
            if isinstance(optional_dependencies, dict):
                for extra_requirements in optional_dependencies.values():
                    if isinstance(extra_requirements, list):
                        requirements.extend(
                            req for req in extra_requirements if isinstance(req, str)
                        )

            for requirement in requirements:
                name = _requirement_name(requirement)
                if name is not None:
                    package_sources[name] = pyproject_path.name

    requirements_path = project_root / "requirements.txt"
    if requirements_path.exists():
        for line in requirements_path.read_text(encoding="utf-8").splitlines():
            stripped = line.split("#", 1)[0].strip()
            if not stripped:
                continue
            name = _requirement_name(stripped)
            if name is not None:
                package_sources.setdefault(name, requirements_path.name)

    if not package_sources:
        return PythonDependencyMetadata(declared_packages=frozenset())

    sources = tuple(dict.fromkeys(package_sources.values()))
    return PythonDependencyMetadata(
        declared_packages=frozenset(package_sources),
        source=sources[0] if len(sources) == 1 else ", ".join(sources),
        package_sources=package_sources,
    )


def _classify_preflight_tool_failure(
    metadata: PythonDependencyMetadata,
    failure: PreflightCommandFailure,
) -> _PreflightToolFailureClassification:
    tool = _configured_tool_name(failure.command, failure.check_name)
    missing_tool = None
    normalized_output = failure.output.strip()
    for pattern in _DECLARED_TOOL_MISSING_PATTERNS:
        match = pattern.search(normalized_output)
        if match is None:
            continue
        missing_tool = _normalize_package_name(match.group("tool"))
        break

    dependency_source = metadata.package_sources.get(tool)
    if missing_tool == tool and dependency_source:
        return MissingDeclaredTool(tool=tool, dependency_source=dependency_source)
    if (
        missing_tool == tool
        and tool in metadata.declared_packages
        and metadata.source.strip()
    ):
        return MissingDeclaredTool(tool=tool, dependency_source=metadata.source)
    return OrdinaryCheckFailure(tool=tool, failure=failure)


def analyze_preflight_command_failures(
    project_root: Path,
    failures: Sequence[PreflightCommandFailure],
) -> tuple[OrdinaryCheckFailure, ...] | SetupPhaseError:
    python_dependency_metadata = load_python_dependency_metadata(project_root)
    ordinary_failures: list[OrdinaryCheckFailure] = []
    for failure in failures:
        classification = _classify_preflight_tool_failure(
            python_dependency_metadata, failure
        )
        if isinstance(classification, MissingDeclaredTool):
            return SetupPhaseError(
                "preflight",
                "Missing expected preflight tool "
                f"'{classification.tool}' declared in "
                f"{classification.dependency_source}.",
                command=failure.command,
                output=failure.output,
            )
        ordinary_failures.append(classification)
    return tuple(ordinary_failures)
