import shutil
import subprocess
import sys
import tarfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import pytest

from pycastle._universal_image_build import UniversalImageBuildRequest
from pycastle.config import Config, StageOverride, resolve_dockerfile
from pycastle.services import DockerService
from pycastle.errors import ConfigValidationError, DockerBuildError, DockerServiceError

_cfg = Config(docker_image_name="test-image")

_BUILDKIT_WITH_REBUILD = [
    "#1 [1/2] FROM python:3.12\n",
    "#1 CACHED\n",
    "#2 [2/2] COPY . .\n",
    "#2 DONE 2.5s\n",
]

_BUILDKIT_ALL_CACHED = [
    "#1 [1/2] FROM python:3.12\n",
    "#1 CACHED\n",
    "#2 [2/2] RUN pip install requests\n",
    "#2 CACHED\n",
]

_CLASSIC_ALL_CACHED = [
    "Step 1/2 : FROM python:3.12\n",
    " ---> Using cache\n",
    " ---> abc123\n",
    "Step 2/2 : RUN pip install requests\n",
    " ---> Using cache\n",
    " ---> def456\n",
    "Successfully built def456\n",
]

_CLASSIC_WITH_REBUILD = [
    "Step 1/2 : FROM python:3.12\n",
    " ---> Using cache\n",
    " ---> abc123\n",
    "Step 2/2 : COPY . .\n",
    " ---> Running in 789abc\n",
    "Successfully built 789abc\n",
]


def _mock_popen(output_lines: list[str], returncode: int = 0) -> MagicMock:
    mock_proc = MagicMock()
    mock_proc.stdout = iter(output_lines)
    mock_proc.wait.return_value = returncode
    mock_proc.returncode = returncode
    return mock_proc


def _make_docker_service(side_effect=None):
    svc = MagicMock()
    if side_effect is not None:
        svc.build.side_effect = side_effect
        svc.build_image.side_effect = side_effect
    else:
        svc.build.return_value = None
        svc.build_image.return_value = None
    return svc


def _built_requests(svc: MagicMock) -> list[UniversalImageBuildRequest]:
    return [call.args[0] for call in svc.build.call_args_list]


def _subprocess_ok():
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    return result


def _cfg_referencing_only(
    service: str, docker_image_name: str = "test-image"
) -> Config:
    override = StageOverride(service=service, effort="low")
    return Config(
        docker_image_name=docker_image_name,
        plan_override=override,
        implement_override=override,
        review_override=override,
        merge_override=override,
        preflight_issue_override=override,
        improve_override=override,
    )


def _shipped_defaults_in_wheel(tmp_path: Path) -> set[str]:
    wheel_members = _wheel_members(tmp_path)
    return {
        name[len("pycastle/") :]
        for name in wheel_members
        if name.startswith("pycastle/defaults/") and not name.endswith("/")
    }


def _wheel_members(tmp_path: Path) -> set[str]:
    repo_root = Path(__file__).resolve().parents[1]
    build_dir = repo_root / "build"
    shutil.rmtree(build_dir, ignore_errors=True)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                ".",
                "--no-deps",
                "-w",
                str(tmp_path),
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        wheel_path = next(tmp_path.glob("pycastle-*.whl"))
        with ZipFile(wheel_path) as wheel:
            return set(wheel.namelist())
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def _shipped_defaults_in_sdist(tmp_path: Path) -> set[str]:
    sdist_members = _sdist_members(tmp_path)
    return {
        name.split("src/pycastle/", 1)[1]
        for name in sdist_members
        if "src/pycastle/defaults/" in name and not name.endswith("/")
    }


def _sdist_members(tmp_path: Path) -> set[str]:
    from setuptools.build_meta import build_sdist  # type: ignore[import-untyped]

    repo_root = Path(__file__).resolve().parents[1]
    build_dir = repo_root / "build"
    shutil.rmtree(build_dir, ignore_errors=True)
    try:
        sdist_name = build_sdist(str(tmp_path))
        with tarfile.open(tmp_path / sdist_name, "r:gz") as sdist:
            return set(sdist.getnames())
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def _bundled_runtime_defaults() -> set[str]:
    return {
        path.relative_to(Path("src/pycastle")).as_posix()
        for path in Path("src/pycastle/defaults").rglob("*")
        if path.is_file() and path.name != ".env"
    }


# ── subprocess command flags ──────────────────────────────────────────────────


def test_main_includes_no_cache_flag(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        main(no_cache=True, docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "--no-cache" in cmd


def test_main_omits_no_cache_flag_by_default(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "--no-cache" not in cmd


# ── python_version extraction ─────────────────────────────────────────────────


def test_main_passes_python_version_from_file(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".python-version").write_text("3.12.1\n")
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "PYTHON_VERSION=3.12" in cmd


def test_main_python_version_short_form_unchanged(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".python-version").write_text("3.12\n")
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "PYTHON_VERSION=3.12" in cmd


def test_main_python_version_single_segment_unchanged(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".python-version").write_text("3\n")
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "PYTHON_VERSION=3" in cmd


def test_main_no_python_version_when_file_absent(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "--build-arg" not in cmd


# ── success / failure outcomes ────────────────────────────────────────────────


def test_main_returns_normally_on_success(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service()
    main(docker_service=svc, cfg=_cfg)


def test_main_propagates_docker_service_error(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service(side_effect=DockerServiceError("docker not found"))
    with pytest.raises(DockerServiceError, match="docker not found"):
        main(docker_service=svc, cfg=_cfg)


def test_main_propagates_docker_build_error(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service(side_effect=DockerBuildError("build failed"))
    with pytest.raises(DockerBuildError, match="build failed"):
        main(docker_service=svc, cfg=_cfg)


# ── default DockerService is created when none provided ──────────────────────


def test_main_creates_default_docker_service(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    with patch("pycastle.commands.build.DockerService") as mock_cls:
        instance = _make_docker_service()
        mock_cls.return_value = instance
        main(cfg=_cfg)
    mock_cls.assert_called_once_with()


# ── Issue 203: cfg injection into build_command.main ─────────────────────────


def test_build_command_uses_docker_image_name_from_cfg(tmp_path, monkeypatch):
    """main(cfg=Config(docker_image_name='myimg', ...)) must pass the universal image name to the typed build request."""
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build.return_value = None

    main(
        docker_service=svc,
        cfg=Config(docker_image_name="myimg"),
    )

    assert [request.image_tag for request in _built_requests(svc)] == ["myimg"]


def test_build_command_builds_one_universal_image_tagged_with_docker_image_name(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build.return_value = None

    main(
        docker_service=svc,
        cfg=Config(docker_image_name="myimg"),
    )

    assert [
        (request.image_tag, request.dockerfile_path, request.context_dir)
        for request in _built_requests(svc)
    ] == [("myimg", resolve_dockerfile(Path("pycastle")), Path("."))]
    assert capsys.readouterr().out.count("Building myimg...") == 1


def test_build_command_passes_typed_request_with_exact_image_tag(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    class _CapturingAdapter:
        def __init__(self) -> None:
            self.requests: list[UniversalImageBuildRequest] = []

        def build(self, request: UniversalImageBuildRequest) -> None:
            self.requests.append(request)

    monkeypatch.chdir(tmp_path)
    adapter = _CapturingAdapter()

    main(
        docker_service=adapter,
        cfg=Config(docker_image_name="registry.example.com/team/pycastle:test-build"),
    )

    assert [request.image_tag for request in adapter.requests] == [
        "registry.example.com/team/pycastle:test-build"
    ]


def test_build_command_uses_resolved_dockerfile_path(tmp_path, monkeypatch):
    """main(cfg=Config(...)) must pass the resolved universal Dockerfile to the typed build request."""
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    dockerfile = pycastle_dir / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    svc = MagicMock()
    svc.build.return_value = None

    main(
        docker_service=svc,
        cfg=Config(docker_image_name="img"),
    )

    assert [request.dockerfile_path for request in _built_requests(svc)] == [
        Path("pycastle/Dockerfile")
    ]


def test_build_command_uses_fixed_project_local_dockerfile_override(
    tmp_path, monkeypatch
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "Dockerfile").write_text("FROM scratch\n")
    svc = MagicMock()
    svc.build.return_value = None

    main(
        docker_service=svc,
        cfg=Config(docker_image_name="img"),
    )

    assert [request.dockerfile_path for request in _built_requests(svc)] == [
        Path("pycastle/Dockerfile")
    ]


# ── Issue 938: universal image builds ────────────────────────────────────────


def test_build_command_builds_universal_image_from_resolved_dockerfile(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    dockerfile = pycastle_dir / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    svc = MagicMock()
    svc.build.return_value = None

    main(docker_service=svc, cfg=Config(docker_image_name="myproject"))

    assert svc.build.call_count == 1
    request = _built_requests(svc)[0]
    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        Path("pycastle/Dockerfile"),
        Path("."),
    )
    assert "Building myproject..." in capsys.readouterr().out


def test_build_command_builds_universal_image_with_default_dockerfile_when_local_override_is_missing(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build.return_value = None

    main(docker_service=svc, cfg=_cfg_referencing_only("codex", "myproject"))

    svc.build.assert_called_once()
    request = _built_requests(svc)[0]
    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        resolve_dockerfile(Path("pycastle")),
        Path("."),
    )
    assert "Building myproject..." in capsys.readouterr().out


def test_build_command_ignores_legacy_project_local_service_specific_dockerfiles(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "Dockerfile.claude").write_text("FROM legacy-claude\n")
    (pycastle_dir / "Dockerfile.codex").write_text("FROM legacy-codex\n")
    (pycastle_dir / "Dockerfile.opencode").write_text("FROM legacy-opencode\n")
    svc = MagicMock()
    svc.build.return_value = None

    main(docker_service=svc, cfg=_cfg_referencing_only("codex", "myproject"))

    svc.build.assert_called_once()
    request = _built_requests(svc)[0]
    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        resolve_dockerfile(Path("pycastle")),
        Path("."),
    )
    assert "Building myproject..." in capsys.readouterr().out


def test_build_command_uses_bundled_default_when_project_local_dockerfile_path_is_a_directory(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "Dockerfile").mkdir()
    bundled_default = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "pycastle"
        / "defaults"
        / "Dockerfile"
    )
    svc = MagicMock()
    svc.build.return_value = None

    main(docker_service=svc, cfg=_cfg_referencing_only("codex", "myproject"))

    svc.build.assert_called_once()
    request = _built_requests(svc)[0]
    assert (request.image_tag, request.dockerfile_path, request.context_dir) == (
        "myproject",
        bundled_default,
        Path("."),
    )
    assert "Building myproject..." in capsys.readouterr().out


def test_build_command_ignores_stage_fallback_services_when_building_universal_image(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / "Dockerfile").write_text("FROM scratch\n")
    svc = MagicMock()
    svc.build.return_value = None
    cfg = Config(
        docker_image_name="myproject",
        plan_override=StageOverride(
            service="claude",
            effort="low",
            fallback=StageOverride(service="codex", effort="low"),
        ),
    )

    main(docker_service=svc, cfg=cfg)

    assert [
        (request.image_tag, request.dockerfile_path, request.context_dir)
        for request in _built_requests(svc)
    ] == [
        ("myproject", Path("pycastle/Dockerfile"), Path(".")),
    ]
    out = capsys.readouterr().out
    assert out.count("Building myproject...") == 1


def test_packaging_includes_bundled_universal_dockerfile():
    package_data = Path("pyproject.toml").read_text(encoding="utf-8")

    assert '"defaults/Dockerfile",' in package_data
    assert '"defaults/Dockerfile.claude",' not in package_data
    assert '"defaults/Dockerfile.codex",' not in package_data
    assert '"defaults/Dockerfile.opencode",' not in package_data


def test_wheel_ships_current_bundled_runtime_defaults_tree_only(tmp_path):
    shipped_defaults = _shipped_defaults_in_wheel(tmp_path)
    bundled_defaults = _bundled_runtime_defaults()

    assert bundled_defaults <= shipped_defaults
    assert "defaults/Dockerfile.claude" not in shipped_defaults
    assert "defaults/Dockerfile.codex" not in shipped_defaults
    assert "defaults/Dockerfile.opencode" not in shipped_defaults


def test_sdist_ships_current_bundled_runtime_defaults_tree_only(tmp_path):
    shipped_defaults = _shipped_defaults_in_sdist(tmp_path)
    bundled_defaults = _bundled_runtime_defaults()

    assert bundled_defaults <= shipped_defaults
    assert "defaults/Dockerfile.claude" not in shipped_defaults
    assert "defaults/Dockerfile.codex" not in shipped_defaults
    assert "defaults/Dockerfile.opencode" not in shipped_defaults


def test_wheel_ships_agent_runtime_package_scaffold(tmp_path):
    wheel_members = _wheel_members(tmp_path)

    assert "pycastle_agent_runtime/__init__.py" in wheel_members
    assert "pycastle_agent_runtime/orchestration.py" in wheel_members
    assert "pycastle_agent_runtime/py.typed" in wheel_members


def test_sdist_ships_agent_runtime_package_scaffold(tmp_path):
    sdist_members = _sdist_members(tmp_path)

    assert any(
        name.endswith("/src/pycastle_agent_runtime/__init__.py")
        for name in sdist_members
    )
    assert any(
        name.endswith("/src/pycastle_agent_runtime/orchestration.py")
        for name in sdist_members
    )
    assert any(
        name.endswith("/src/pycastle_agent_runtime/py.typed") for name in sdist_members
    )


def test_agent_runtime_package_exports_the_runtime_surface():
    import pycastle_agent_runtime as runtime

    from pycastle.agents.runner import AgentRunner, AgentRunnerProtocol, RunRequest
    from pycastle.config.types import StageOverride
    from pycastle.services.agent_service import (
        AgentService,
        AssistantTurn,
        CredentialFailure,
        HardError,
        ParsedTurn,
        PromptTokens,
        Result,
        TransientError,
        UnsupportedTokens,
        UsageLimit,
    )
    from pycastle.services import ServiceRegistry

    assert runtime.AgentRunner is AgentRunner
    assert runtime.AgentRunnerProtocol is AgentRunnerProtocol
    assert runtime.AgentService is AgentService
    assert runtime.AssistantTurn is AssistantTurn
    assert runtime.CredentialFailure is CredentialFailure
    assert runtime.HardError is HardError
    assert runtime.ParsedTurn == ParsedTurn
    assert runtime.PromptTokens is PromptTokens
    assert runtime.Result is Result
    assert runtime.RunRequest is RunRequest
    assert runtime.ServiceRegistry is ServiceRegistry
    assert runtime.StageOverride is StageOverride
    assert runtime.TransientError is TransientError
    assert runtime.UnsupportedTokens is UnsupportedTokens
    assert runtime.UsageLimit is UsageLimit


def test_local_distribution_installs_importable_agent_runtime_package(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    install_dir = tmp_path / "site-packages"
    build_dir = repo_root / "build"
    shutil.rmtree(build_dir, ignore_errors=True)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                ".",
                "--no-deps",
                "--target",
                str(install_dir),
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        env = dict(os.environ)
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(install_dir)
            if not existing_pythonpath
            else f"{install_dir}{os.pathsep}{existing_pythonpath}"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import pycastle_agent_runtime as runtime; "
                    "print(runtime.AgentRunner.__name__); "
                    "print(runtime.ServiceRegistry.__name__); "
                    "print(runtime.run.__module__)"
                ),
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.stdout.splitlines() == [
            "AgentRunner",
            "ServiceRegistry",
            "pycastle_agent_runtime.orchestration",
        ]
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def test_bundled_universal_dockerfile_installs_supported_clis_and_baseline_tools():
    dockerfile = Path("src/pycastle/defaults/Dockerfile").read_text(encoding="utf-8")

    assert "@openai/codex@0.134.0" in dockerfile
    assert "@anthropic-ai/claude-code@2.1.152" in dockerfile
    assert "opencode-ai@1.15.12" in dockerfile
    assert 'ENV PATH="/home/agent/.local/bin:$PATH"' in dockerfile
    for tool in ("gh", "git", "jq", "curl", "ripgrep"):
        assert tool in dockerfile
    assert "GH_TOKEN" not in dockerfile
    assert "auth.json" not in dockerfile


def test_adr_0029_is_kept_as_superseded_historical_context_for_universal_dockerfile():
    adr = Path("docs/adr/0029-per-service-docker-images.md").read_text(encoding="utf-8")

    assert "Superseded by ADR 0034" in adr
    assert "historical context" in adr.lower()
    assert "universal agent image" in adr.lower()


# ── Issue 222: empty docker_image_name guard ──────────────────────────────────


def test_build_command_raises_when_docker_image_name_is_empty(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()

    with pytest.raises(ConfigValidationError):
        main(
            docker_service=svc,
            cfg=Config(docker_image_name=""),
        )


def test_build_command_empty_docker_image_name_prints_helpful_message(
    tmp_path, monkeypatch
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()

    with pytest.raises(ConfigValidationError) as exc_info:
        main(
            docker_service=svc,
            cfg=Config(docker_image_name=""),
        )

    msg = str(exc_info.value)
    assert "docker_image_name" in msg
    assert "pycastle init" in msg


def test_build_command_empty_docker_image_name_does_not_call_docker(
    tmp_path, monkeypatch
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()

    with pytest.raises(ConfigValidationError):
        main(
            docker_service=svc,
            cfg=Config(docker_image_name=""),
        )

    svc.build.assert_not_called()


# ── Issue 223: success message on build ──────────────────────────────────────


def test_main_prints_success_message_to_stdout_on_success(
    tmp_path, monkeypatch, capsys
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service()
    main(docker_service=svc, cfg=_cfg)
    out = capsys.readouterr().out
    assert "Build complete" in out


def test_main_does_not_print_success_message_on_failure(tmp_path, monkeypatch, capsys):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service(side_effect=DockerServiceError("build failed"))
    with pytest.raises(DockerServiceError):
        main(docker_service=svc, cfg=_cfg)
    out = capsys.readouterr().out
    assert "Build complete" not in out


def test_main_success_message_not_on_stderr(tmp_path, monkeypatch, capsys):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service()
    main(docker_service=svc, cfg=_cfg)
    err = capsys.readouterr().err
    assert "Build complete" not in err


# ── terse mode passes terse=True to the service ──────────────────────────────


def test_terse_mode_passes_terse_flag_to_service(tmp_path, monkeypatch):
    """stream=True, terse=True is forwarded to the typed build request options."""
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build.return_value = None
    main(stream=True, terse=True, docker_service=svc, cfg=_cfg)
    assert _built_requests(svc)[0].options.terse is True


def test_terse_mode_does_not_print_image_up_to_date(tmp_path, monkeypatch, capsys):
    """With terse=True the build command doesn't print 'Image up to date'."""
    from pycastle.commands.build import main
    from pycastle.services.docker_service import BuildOutcome

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build.return_value = BuildOutcome.FULL_CACHE_HIT
    main(stream=True, terse=True, docker_service=svc, cfg=_cfg)
    out = capsys.readouterr().out
    assert "Image up to date" not in out


def test_non_terse_stream_still_prints_image_up_to_date(tmp_path, monkeypatch, capsys):
    """stream=True without terse still prints 'Image up to date' on full cache hit."""
    from pycastle.commands.build import main
    from pycastle.services.docker_service import BuildOutcome

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build.return_value = BuildOutcome.FULL_CACHE_HIT
    main(stream=True, terse=False, docker_service=svc, cfg=_cfg)
    out = capsys.readouterr().out
    assert "Image up to date" in out
