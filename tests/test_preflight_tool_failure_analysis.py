from pathlib import Path

from pycastle.errors import SetupPhaseError
from pycastle.infrastructure.preflight_tool_classifier import (
    setup_phase_error_for_preflight_command_failures,
)
from pycastle.preflight_tool_failure_analysis import (
    MissingDeclaredTool,
    OrdinaryCheckFailure,
    PreflightCommandFailure,
    PythonDependencyMetadata,
    classify_preflight_tool_failure,
    load_python_dependency_metadata,
)


def test_load_python_dependency_metadata_reads_optional_dependencies_from_pyproject(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "dependencies = ['click']\n"
        "[project.optional-dependencies]\n"
        "dev = ['ruff', 'mypy']\n",
        encoding="utf-8",
    )

    metadata = load_python_dependency_metadata(tmp_path)

    assert metadata.declared_packages == frozenset({"click", "ruff", "mypy"})
    assert metadata.source == "pyproject.toml"


def test_load_python_dependency_metadata_falls_back_to_requirements_when_pyproject_has_no_project_table(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n")
    (tmp_path / "requirements.txt").write_text("ruff==0.6.0\n", encoding="utf-8")

    metadata = load_python_dependency_metadata(tmp_path)

    assert metadata.declared_packages == frozenset({"ruff"})
    assert metadata.source == "requirements.txt"


def test_load_python_dependency_metadata_tracks_requirements_source_per_package(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['click']\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("ruff==0.6.0\n", encoding="utf-8")

    metadata = load_python_dependency_metadata(tmp_path)

    assert metadata.declared_packages == frozenset({"click", "ruff"})
    assert metadata.package_sources["click"] == "pyproject.toml"
    assert metadata.package_sources["ruff"] == "requirements.txt"


def test_classify_preflight_tool_failure_marks_declared_python_module_as_missing_tool() -> (
    None
):
    classification = classify_preflight_tool_failure(
        PythonDependencyMetadata(
            declared_packages=frozenset({"ruff"}),
            source="pyproject.toml",
        ),
        PreflightCommandFailure(
            check_name="ruff",
            command="python -m ruff check .",
            output="Command failed (exit 1): /usr/bin/python: No module named ruff",
        ),
    )

    assert classification == MissingDeclaredTool(
        tool="ruff",
        dependency_source="pyproject.toml",
    )


def test_classify_preflight_tool_failure_keeps_undeclared_missing_tool_as_ordinary_check_failure() -> (
    None
):
    classification = classify_preflight_tool_failure(
        PythonDependencyMetadata(
            declared_packages=frozenset({"pytest"}),
            source="pyproject.toml",
        ),
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        ),
    )

    assert classification == OrdinaryCheckFailure(tool="ruff")


def test_preflight_tool_classifier_returns_setup_failure_for_missing_pyproject_declared_command(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )

    result = setup_phase_error_for_preflight_command_failures(
        tmp_path,
        (
            PreflightCommandFailure(
                check_name="ruff",
                command="ruff check .",
                output="Command failed (exit 127): bash: ruff: command not found",
            ),
        ),
    )

    assert isinstance(result, SetupPhaseError)
    assert result.phase == "preflight"
    assert (
        str(result)
        == "Missing expected preflight tool 'ruff' declared in pyproject.toml."
    )
    assert result.command == "ruff check ."
    assert result.output == "Command failed (exit 127): bash: ruff: command not found"
