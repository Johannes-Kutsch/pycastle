from __future__ import annotations

import dataclasses
import re
import shlex
import tomllib
from pathlib import Path

_DECLARED_TOOL_MISSING_PATTERNS = (
    re.compile(r"\b(?P<tool>[A-Za-z0-9_.-]+): command not found\b", re.IGNORECASE),
    re.compile(
        r"\bNo module named ['\"]?(?P<tool>[A-Za-z0-9_.-]+)['\"]?\b", re.IGNORECASE
    ),
)
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


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
    if parts:
        return _normalize_package_name(Path(parts[0]).name)
    return _normalize_package_name(check_name)


@dataclasses.dataclass(frozen=True)
class PythonDependencyMetadata:
    declared_packages: frozenset[str]
    source: str = ""


@dataclasses.dataclass(frozen=True)
class PreflightCommandFailure:
    check_name: str
    command: str
    output: str


@dataclasses.dataclass(frozen=True)
class OrdinaryCheckFailure:
    tool: str


@dataclasses.dataclass(frozen=True)
class MissingDeclaredTool:
    tool: str
    dependency_source: str


PreflightToolFailureClassification = OrdinaryCheckFailure | MissingDeclaredTool


def load_python_dependency_metadata(project_root: Path) -> PythonDependencyMetadata:
    pyproject_path = project_root / "pyproject.toml"
    if pyproject_path.exists():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data.get("project")
        if not isinstance(project, dict):
            return PythonDependencyMetadata(declared_packages=frozenset())

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

        return PythonDependencyMetadata(
            declared_packages=frozenset(
                name
                for req in requirements
                if (name := _requirement_name(req)) is not None
            ),
            source=pyproject_path.name,
        )

    requirements_path = project_root / "requirements.txt"
    if not requirements_path.exists():
        return PythonDependencyMetadata(declared_packages=frozenset())

    requirements = []
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            requirements.append(stripped)
    return PythonDependencyMetadata(
        declared_packages=frozenset(
            name for req in requirements if (name := _requirement_name(req)) is not None
        ),
        source=requirements_path.name,
    )


def classify_preflight_tool_failure(
    metadata: PythonDependencyMetadata,
    failure: PreflightCommandFailure,
) -> PreflightToolFailureClassification:
    tool = _configured_tool_name(failure.command, failure.check_name)
    missing_tool = None
    normalized_output = failure.output.strip()
    for pattern in _DECLARED_TOOL_MISSING_PATTERNS:
        match = pattern.search(normalized_output)
        if match is None:
            continue
        missing_tool = _normalize_package_name(match.group("tool"))
        break

    if (
        missing_tool == tool
        and tool in metadata.declared_packages
        and metadata.source.strip()
    ):
        return MissingDeclaredTool(tool=tool, dependency_source=metadata.source)
    return OrdinaryCheckFailure(tool=tool)
