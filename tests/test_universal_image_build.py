from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from pycastle._universal_image_build import (
    UniversalImageBuildAdapter,
    UniversalImageBuildOptions,
    UniversalImageBuildRequest,
    build_universal_image,
)
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
