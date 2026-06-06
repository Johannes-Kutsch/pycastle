from pathlib import Path

import pytest

from pycastle.errors import SetupPhaseError
from pycastle.infrastructure.preflight_failure_interpreter import (
    MissingDeclaredPythonToolDecision,
    OrdinaryCheckFailure,
    OrdinaryPreflightFailureDecision,
    PreflightCommandFailure,
    analyze_preflight_command_failures,
    interpret_preflight_command_failures,
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


def test_load_python_dependency_metadata_prefers_pyproject_source_when_tool_declared_in_both_metadata_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['ruff>=0.6']\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("ruff==0.6.0\n", encoding="utf-8")

    metadata = load_python_dependency_metadata(tmp_path)

    assert metadata.declared_packages == frozenset({"ruff"})
    assert metadata.source == "pyproject.toml"
    assert metadata.package_sources["ruff"] == "pyproject.toml"


def test_analyze_preflight_command_failures_returns_setup_failure_for_missing_requirements_declared_command_with_mixed_metadata(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['click>=8']\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("ruff==0.6.0\n", encoding="utf-8")

    result = analyze_preflight_command_failures(
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
        == "Missing expected preflight tool 'ruff' declared in requirements.txt."
    )
    assert result.command == "ruff check ."
    assert result.output == "Command failed (exit 127): bash: ruff: command not found"


def test_analyze_preflight_command_failures_returns_setup_failure_with_pyproject_precedence_when_tool_declared_in_both_metadata_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("ruff==0.6.0\n", encoding="utf-8")

    result = analyze_preflight_command_failures(
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


def test_analyze_preflight_command_failures_keeps_undeclared_missing_tool_as_ordinary_check_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['pytest>=8']\n",
        encoding="utf-8",
    )
    failure = PreflightCommandFailure(
        check_name="ruff",
        command="ruff check .",
        output="Command failed (exit 127): bash: ruff: command not found",
    )

    result = analyze_preflight_command_failures(tmp_path, (failure,))

    assert result == (OrdinaryCheckFailure(tool="ruff", failure=failure),)


def test_analyze_preflight_command_failures_returns_setup_failure_for_missing_pyproject_declared_command(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )

    result = analyze_preflight_command_failures(
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


def test_analyze_preflight_command_failures_keeps_declared_source_quality_failure_as_ordinary(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n",
        encoding="utf-8",
    )
    failure = PreflightCommandFailure(
        check_name="ruff",
        command="ruff check .",
        output="src/demo.py:1:1: F401 `os` imported but unused",
    )

    result = analyze_preflight_command_failures(tmp_path, (failure,))

    assert result == (OrdinaryCheckFailure(tool="ruff", failure=failure),)


def test_interpret_preflight_command_failures_returns_typed_decisions(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n",
        encoding="utf-8",
    )
    failures = (
        PreflightCommandFailure(
            check_name="format",
            command="black --check .",
            output="would reformat src/demo.py",
        ),
        PreflightCommandFailure(
            check_name="lint",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        ),
    )

    result = interpret_preflight_command_failures(tmp_path, failures)

    assert result == (
        OrdinaryPreflightFailureDecision(
            check_name="format",
            command="black --check .",
            output="would reformat src/demo.py",
            tool="black",
        ),
        MissingDeclaredPythonToolDecision(
            check_name="lint",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
            tool="ruff",
            dependency_source="pyproject.toml",
        ),
    )


def test_interpret_preflight_command_failures_returns_empty_for_no_failures(
    tmp_path: Path,
) -> None:
    result = interpret_preflight_command_failures(tmp_path, ())

    assert result == ()


@pytest.mark.parametrize(
    "command, output",
    [
        (
            "python -X dev -m ruff check .",
            "Command failed (exit 1): /usr/bin/python: No module named ruff",
        ),
        (
            "python3 -m ruff check .",
            "Command failed (exit 1): /usr/bin/python3: No module named ruff",
        ),
        (
            "py -m ruff check .",
            "Command failed (exit 1): C:\\Python312\\python.exe: No module named ruff",
        ),
        (
            "py -3 -m ruff check .",
            "Command failed (exit 1): C:\\Python312\\python.exe: No module named ruff",
        ),
    ],
)
def test_analyze_preflight_command_failures_uses_python_module_name_for_launcher_variants(
    tmp_path: Path, command: str, output: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\ndependencies = ['ruff>=0.5']\n",
        encoding="utf-8",
    )

    result = analyze_preflight_command_failures(
        tmp_path,
        (
            PreflightCommandFailure(
                check_name="lint",
                command=command,
                output=output,
            ),
        ),
    )

    assert isinstance(result, SetupPhaseError)
    assert result.phase == "preflight"
    assert (
        str(result)
        == "Missing expected preflight tool 'ruff' declared in pyproject.toml."
    )
    assert result.command == command
    assert result.output == output
