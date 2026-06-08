from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pycastle._universal_image_build import (
    UniversalImageBuildAdapter,
    UniversalImageBuildOptions,
    UniversalImageBuildRequest,
)
from pycastle.services._docker_build_output import BuildOutcome
from pycastle.services.docker_service import DockerServiceUniversalImageBuildAdapter


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


@dataclass
class _RecordingDockerService:
    outcome: BuildOutcome | None
    calls: list[dict[str, object]] = field(default_factory=list)

    def build_image(
        self,
        image_name: str,
        dockerfile_path: Path,
        context_dir: Path,
        *,
        no_cache: bool = False,
        python_version: str | None = None,
        timeout: float | None = None,
        stream: bool = False,
        terse: bool = False,
        on_rebuild_start: object = None,
    ) -> BuildOutcome | None:
        self.calls.append(
            {
                "image_name": image_name,
                "dockerfile_path": dockerfile_path,
                "context_dir": context_dir,
                "no_cache": no_cache,
                "python_version": python_version,
                "timeout": timeout,
                "stream": stream,
                "terse": terse,
                "on_rebuild_start": on_rebuild_start,
            }
        )
        return self.outcome


def test_docker_service_universal_image_build_adapter_forwards_typed_request_fields(
    tmp_path,
):
    def callback() -> None:
        return None

    docker_service = _RecordingDockerService(BuildOutcome.REBUILT)
    adapter = DockerServiceUniversalImageBuildAdapter(
        docker_service,
        timeout=42.0,
        on_rebuild_start=callback,
    )
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

    result = _build_with_adapter(adapter, request)

    assert result == BuildOutcome.REBUILT
    assert docker_service.calls == [
        {
            "image_name": "pycastle:test",
            "dockerfile_path": tmp_path / "Dockerfile",
            "context_dir": tmp_path,
            "no_cache": True,
            "python_version": "3.12",
            "timeout": 42.0,
            "stream": True,
            "terse": True,
            "on_rebuild_start": callback,
        }
    ]
