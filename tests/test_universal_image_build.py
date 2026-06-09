from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle._universal_image_build import (
    UniversalImageBuildAdapter,
    UniversalImageBuildOptions,
    UniversalImageBuildRequest,
    build_universal_image,
    resolve_universal_image_build_request,
)
from pycastle.config import Config
from pycastle.config.types import StageOverride
from pycastle.errors import ConfigValidationError
from pycastle.services._docker_build_output import BuildOutcome, FINAL_OUTCOME_EXAMPLES
from pycastle.services.docker_service import DockerService


def _build_with_adapter(
    adapter: UniversalImageBuildAdapter,
    request: UniversalImageBuildRequest,
) -> BuildOutcome | None:
    return adapter.build(request)


def test_universal_image_build_request_defaults_match_non_streaming_build(tmp_path):
    request = UniversalImageBuildRequest(
        image_tag="pycastle:latest",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )

    assert request.options == UniversalImageBuildOptions()


@dataclass
class _FakeUniversalImageBuildAdapter:
    outcome: BuildOutcome | None
    requests: list[UniversalImageBuildRequest] = field(default_factory=list)

    def build(self, request: UniversalImageBuildRequest) -> BuildOutcome | None:
        self.requests.append(request)
        return self.outcome


def test_universal_image_build_adapter_protocol_accepts_fake_adapter(tmp_path):
    request = UniversalImageBuildRequest(
        image_tag="pycastle:cached",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )
    adapter = _FakeUniversalImageBuildAdapter(BuildOutcome.FULL_CACHE_HIT)

    result = _build_with_adapter(adapter, request)

    assert result == BuildOutcome.FULL_CACHE_HIT
    assert adapter.requests == [request]


def test_universal_image_build_rejects_empty_image_tag_before_adapter(tmp_path):
    request = UniversalImageBuildRequest(
        image_tag="",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )
    adapter = _FakeUniversalImageBuildAdapter(BuildOutcome.FULL_CACHE_HIT)

    with pytest.raises(ConfigValidationError) as exc_info:
        build_universal_image(adapter, request)

    assert str(exc_info.value) == (
        "docker_image_name is not set. Run `pycastle init` to configure your project."
    )
    assert adapter.requests == []


def test_universal_image_build_passes_exact_image_tag_to_adapter(tmp_path):
    request = UniversalImageBuildRequest(
        image_tag="registry.example.com/team/pycastle:test-build",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )
    adapter = _FakeUniversalImageBuildAdapter(BuildOutcome.FULL_CACHE_HIT)

    result = build_universal_image(adapter, request)

    assert result == BuildOutcome.FULL_CACHE_HIT
    assert adapter.requests == [request]


def test_universal_image_build_prints_building_line_before_adapter_runs(
    tmp_path, capsys
):
    request = UniversalImageBuildRequest(
        image_tag="pycastle:test-build",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )

    class _ObservingAdapter:
        def build(self, request: UniversalImageBuildRequest) -> BuildOutcome | None:
            assert capsys.readouterr().out == "Building pycastle:test-build...\n"
            return BuildOutcome.REBUILT

    result = build_universal_image(_ObservingAdapter(), request)

    assert result == BuildOutcome.REBUILT


def test_universal_image_build_prints_build_complete_for_non_stream_success(
    tmp_path, capsys
):
    request = UniversalImageBuildRequest(
        image_tag="pycastle:test-build",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )
    adapter = _FakeUniversalImageBuildAdapter(BuildOutcome.REBUILT)

    result = build_universal_image(adapter, request)

    assert result == BuildOutcome.REBUILT
    assert capsys.readouterr().out == (
        "Building pycastle:test-build...\nBuild complete.\n"
    )


def test_universal_image_build_prints_image_up_to_date_for_non_terse_stream_cache_hit(
    tmp_path, capsys
):
    request = UniversalImageBuildRequest(
        image_tag="pycastle:test-build",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
        options=UniversalImageBuildOptions(stream=True, terse=False),
    )
    adapter = _FakeUniversalImageBuildAdapter(BuildOutcome.FULL_CACHE_HIT)

    result = build_universal_image(adapter, request)

    assert result == BuildOutcome.FULL_CACHE_HIT
    assert capsys.readouterr().out == (
        "Building pycastle:test-build...\nImage up to date.\n"
    )


def test_universal_image_build_does_not_print_image_up_to_date_for_terse_stream_cache_hit(
    tmp_path, capsys
):
    request = UniversalImageBuildRequest(
        image_tag="pycastle:test-build",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
        options=UniversalImageBuildOptions(stream=True, terse=True),
    )
    adapter = _FakeUniversalImageBuildAdapter(BuildOutcome.FULL_CACHE_HIT)

    result = build_universal_image(adapter, request)

    assert result == BuildOutcome.FULL_CACHE_HIT
    assert capsys.readouterr().out == "Building pycastle:test-build...\n"


@pytest.mark.parametrize(
    ("options", "expected_output"),
    [
        (UniversalImageBuildOptions(), "Building pycastle:test-build...\n"),
        (
            UniversalImageBuildOptions(stream=True, terse=False),
            "Building pycastle:test-build...\n",
        ),
    ],
)
def test_universal_image_build_does_not_print_success_summary_on_adapter_failure(
    tmp_path, capsys, options, expected_output
):
    request = UniversalImageBuildRequest(
        image_tag="pycastle:test-build",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
        options=options,
    )
    adapter = _FakeUniversalImageBuildAdapter(BuildOutcome.FULL_CACHE_HIT)
    adapter.build = MagicMock(side_effect=RuntimeError("adapter failed"))

    with pytest.raises(RuntimeError, match="adapter failed"):
        build_universal_image(adapter, request)

    assert capsys.readouterr().out == expected_output


def test_resolve_universal_image_build_request_uses_local_override_and_project_root(
    tmp_path,
):
    project_root = tmp_path / "project"
    pycastle_dir = project_root / "pycastle"
    pycastle_dir.mkdir(parents=True)
    dockerfile = pycastle_dir / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        dockerfile,
        project_root,
    )


def test_resolve_universal_image_build_request_uses_bundled_default_when_local_override_is_missing(
    tmp_path,
):
    project_root = tmp_path / "project"
    (project_root / "pycastle").mkdir(parents=True)
    bundled_default = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "pycastle"
        / "defaults"
        / "Dockerfile"
    )

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        bundled_default,
        project_root,
    )


def test_resolve_universal_image_build_request_falls_back_when_project_local_dockerfile_path_is_a_directory(
    tmp_path,
):
    project_root = tmp_path / "project"
    pycastle_dir = project_root / "pycastle"
    pycastle_dir.mkdir(parents=True)
    (pycastle_dir / "Dockerfile").mkdir()
    bundled_default = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "pycastle"
        / "defaults"
        / "Dockerfile"
    )

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        bundled_default,
        project_root,
    )


def test_resolve_universal_image_build_request_ignores_legacy_per_service_dockerfiles(
    tmp_path,
):
    project_root = tmp_path / "project"
    pycastle_dir = project_root / "pycastle"
    pycastle_dir.mkdir(parents=True)
    (pycastle_dir / "Dockerfile.claude").write_text("FROM legacy-claude\n")
    (pycastle_dir / "Dockerfile.codex").write_text("FROM legacy-codex\n")
    (pycastle_dir / "Dockerfile.opencode").write_text("FROM legacy-opencode\n")
    bundled_default = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "pycastle"
        / "defaults"
        / "Dockerfile"
    )

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        bundled_default,
        project_root,
    )


def test_resolve_universal_image_build_request_ignores_stage_priority_chain_when_selecting_build_inputs(
    tmp_path,
):
    project_root = tmp_path / "project"
    pycastle_dir = project_root / "pycastle"
    pycastle_dir.mkdir(parents=True)
    dockerfile = pycastle_dir / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    cfg = Config(
        docker_image_name="myproject",
        plan_override=StageOverride(
            service="claude",
            model="haiku",
            effort="low",
            fallback=StageOverride(
                service="codex",
                model="gpt-5.4-mini",
                effort="low",
                fallback=StageOverride(
                    service="opencode",
                    model="deepseek-v4-flash",
                    effort="medium",
                ),
            ),
        ),
        implement_override=StageOverride(
            service="opencode",
            model="deepseek-v4-flash",
            effort="medium",
        ),
    )

    request = resolve_universal_image_build_request(cfg, project_root=project_root)

    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        dockerfile,
        project_root,
    )


def test_resolve_universal_image_build_request_reads_python_version_from_project_root_and_normalizes_patch_level(
    tmp_path,
):
    project_root = tmp_path / "project"
    (project_root / "pycastle").mkdir(parents=True)
    (project_root / ".python-version").write_text(" 3.12.1 \n")
    (tmp_path / ".python-version").write_text("9.9.9\n")

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert request.options.python_version == "3.12"


def test_resolve_universal_image_build_request_keeps_major_minor_python_version(
    tmp_path,
):
    project_root = tmp_path / "project"
    (project_root / "pycastle").mkdir(parents=True)
    (project_root / ".python-version").write_text("3.12\n")

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert request.options.python_version == "3.12"


def test_resolve_universal_image_build_request_keeps_single_segment_python_version(
    tmp_path,
):
    project_root = tmp_path / "project"
    (project_root / "pycastle").mkdir(parents=True)
    (project_root / ".python-version").write_text("3\n")

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert request.options.python_version == "3"


def test_resolve_universal_image_build_request_omits_python_version_when_file_is_absent(
    tmp_path,
):
    project_root = tmp_path / "project"
    (project_root / "pycastle").mkdir(parents=True)

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
    )

    assert request.options.python_version is None


def test_resolve_universal_image_build_request_keeps_explicit_python_version_option(
    tmp_path,
):
    project_root = tmp_path / "project"
    (project_root / "pycastle").mkdir(parents=True)
    (project_root / ".python-version").write_text("3.12.1\n")

    request = resolve_universal_image_build_request(
        Config(docker_image_name="myproject"),
        project_root=project_root,
        options=UniversalImageBuildOptions(
            python_version="3.11",
            no_cache=True,
            stream=True,
            terse=True,
        ),
    )

    assert request.options == UniversalImageBuildOptions(
        python_version="3.11",
        no_cache=True,
        stream=True,
        terse=True,
    )


def _mock_proc(output_lines: tuple[str, ...], *, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = iter(output_lines)
    proc.wait.return_value = returncode
    proc.returncode = returncode
    return proc


def test_docker_service_build_uses_verbose_stream_mode_for_typed_request(
    tmp_path, capsys
):
    rebuild_started: list[str] = []

    def on_rebuild_start() -> None:
        rebuild_started.append("called")

    docker_service = DockerService(timeout=42.0, on_rebuild_start=on_rebuild_start)
    request = UniversalImageBuildRequest(
        image_tag="pycastle:test",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
        options=UniversalImageBuildOptions(
            python_version="3.12",
            no_cache=True,
            stream=True,
        ),
    )
    proc = _mock_proc(FINAL_OUTCOME_EXAMPLES["buildkit_rebuilt"].lines)

    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=proc,
    ) as mock_popen:
        result = _build_with_adapter(docker_service, request)

    assert result == BuildOutcome.REBUILT
    assert rebuild_started == ["called"]
    proc.wait.assert_called_once_with(timeout=42.0)
    assert "COPY . ." in capsys.readouterr().out
    mock_popen.assert_called_once_with(
        [
            "docker",
            "build",
            "--no-cache",
            "-t",
            "pycastle:test",
            "-f",
            str(tmp_path / "Dockerfile"),
            "--build-arg",
            "PYTHON_VERSION=3.12",
            str(tmp_path),
        ],
        stdout=-1,
        stderr=-2,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_docker_service_build_uses_terse_mode_for_typed_request(tmp_path, capsys):
    docker_service = DockerService(timeout=42.0)
    request = UniversalImageBuildRequest(
        image_tag="pycastle:test",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
        options=UniversalImageBuildOptions(
            python_version="3.12",
            no_cache=True,
            stream=True,
            terse=True,
        ),
    )
    proc = _mock_proc(FINAL_OUTCOME_EXAMPLES["buildkit_all_cached"].lines)

    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=proc,
    ) as mock_popen:
        result = _build_with_adapter(docker_service, request)

    assert result == BuildOutcome.FULL_CACHE_HIT
    proc.wait.assert_called_once_with(timeout=42.0)
    captured = capsys.readouterr().out
    assert "Building Docker Image" in captured
    assert "CACHED" not in captured
    mock_popen.assert_called_once_with(
        [
            "docker",
            "build",
            "--no-cache",
            "-t",
            "pycastle:test",
            "-f",
            str(tmp_path / "Dockerfile"),
            "--build-arg",
            "PYTHON_VERSION=3.12",
            str(tmp_path),
        ],
        stdout=-1,
        stderr=-2,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_docker_service_build_rejects_empty_image_tag_without_starting_docker(
    tmp_path,
):
    docker_service = DockerService()
    request = UniversalImageBuildRequest(
        image_tag="",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )

    with patch("pycastle.services.docker_service.subprocess.run") as mock_run:
        with pytest.raises(ValueError, match="image_name must not be empty"):
            docker_service.build(request)

    mock_run.assert_not_called()
