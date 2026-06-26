import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import pytest

from pycastle._universal_image_build import (
    UniversalImageBuildOptions,
    UniversalImageBuildRequest,
)
from pycastle.config import Config
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


def _make_docker_service(side_effect=None):
    svc = MagicMock()
    if side_effect is not None:
        svc.build.side_effect = side_effect
        svc.build_image.side_effect = side_effect
    else:
        svc.build.return_value = None
        svc.build_image.return_value = None
    return svc


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


# ── build command delegation ──────────────────────────────────────────────────


def test_build_command_delegates_to_universal_image_build_with_explicit_inputs(
    tmp_path, monkeypatch
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    docker_service = MagicMock()
    options = UniversalImageBuildOptions(no_cache=True, stream=True, terse=True)
    request = UniversalImageBuildRequest(
        image_tag="test-image",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
        options=options,
    )

    with (
        patch(
            "pycastle.commands.build.resolve_universal_image_build_request",
            return_value=request,
        ) as mock_resolve,
        patch("pycastle.commands.build.build_universal_image") as mock_build,
    ):
        main(options=options, docker_service=docker_service, cfg=_cfg)

    mock_resolve.assert_called_once_with(_cfg, project_root=Path("."), options=options)
    mock_build.assert_called_once_with(docker_service, request)


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
    request = UniversalImageBuildRequest(
        image_tag="test-image",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )

    with (
        patch("pycastle.commands.build.DockerService") as mock_cls,
        patch(
            "pycastle.commands.build.resolve_universal_image_build_request",
            return_value=request,
        ),
        patch("pycastle.commands.build.build_universal_image") as mock_build,
    ):
        instance = _make_docker_service()
        mock_cls.return_value = instance
        main(cfg=_cfg)

    mock_cls.assert_called_once_with()
    mock_build.assert_called_once_with(instance, request)


def test_build_command_loads_config_when_cfg_is_absent(tmp_path, monkeypatch):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="loaded-from-disk")
    request = UniversalImageBuildRequest(
        image_tag="loaded-from-disk",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )
    docker_service = MagicMock()

    with (
        patch("pycastle.commands.build.load_config", return_value=cfg) as mock_load,
        patch(
            "pycastle.commands.build.resolve_universal_image_build_request",
            return_value=request,
        ) as mock_resolve,
        patch("pycastle.commands.build.build_universal_image") as mock_build,
    ):
        main(docker_service=docker_service)

    mock_load.assert_called_once_with()
    mock_resolve.assert_called_once_with(
        cfg, project_root=Path("."), options=UniversalImageBuildOptions()
    )
    mock_build.assert_called_once_with(docker_service, request)


def test_build_command_prefers_explicit_cfg_without_loading_config(
    tmp_path, monkeypatch
):
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="passed-explicitly")
    request = UniversalImageBuildRequest(
        image_tag="passed-explicitly",
        dockerfile_path=tmp_path / "Dockerfile",
        context_dir=tmp_path,
    )
    docker_service = MagicMock()

    with (
        patch("pycastle.commands.build.load_config") as mock_load,
        patch(
            "pycastle.commands.build.resolve_universal_image_build_request",
            return_value=request,
        ) as mock_resolve,
        patch("pycastle.commands.build.build_universal_image") as mock_build,
    ):
        main(docker_service=docker_service, cfg=cfg)

    mock_load.assert_not_called()
    mock_resolve.assert_called_once_with(
        cfg, project_root=Path("."), options=UniversalImageBuildOptions()
    )
    mock_build.assert_called_once_with(docker_service, request)


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


def test_agent_runtime_package_exports_the_runtime_surface():
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
    from pycastle.services.service_registry import ServiceRegistry
    from pycastle.runtime_session import (
        ProviderSessionState,
        ProviderSessionStateRequest,
        RunKind,
    )
    from pycastle.stage_priority_chain import (
        ChainEntry,
        ConfiguredCandidateSelection,
        iter_stage_chain,
    )
    from pycastle.config.types import StageOverride
    from pycastle.usage_limit_decision import (
        ContinueNow,
        PermanentlyExhausted,
        SleepUntil,
        Stop,
        TemporaryUsageLimit,
        UsageLimitContinuationDecision,
        decide_usage_limit_continuation,
    )
    from pycastle.runtime import __all__ as runtime_all

    runtime = SimpleNamespace(
        AgentService=AgentService,
        AssistantTurn=AssistantTurn,
        ChainEntry=ChainEntry,
        ConfiguredCandidateSelection=ConfiguredCandidateSelection,
        ContinueNow=ContinueNow,
        CredentialFailure=CredentialFailure,
        HardError=HardError,
        ParsedTurn=ParsedTurn,
        PromptTokens=PromptTokens,
        ProviderSessionState=ProviderSessionState,
        ProviderSessionStateRequest=ProviderSessionStateRequest,
        Result=Result,
        RunKind=RunKind,
        ServiceRegistry=ServiceRegistry,
        SleepUntil=SleepUntil,
        StageOverride=StageOverride,
        Stop=Stop,
        PermanentlyExhausted=PermanentlyExhausted,
        TemporaryUsageLimit=TemporaryUsageLimit,
        TransientError=TransientError,
        UnsupportedTokens=UnsupportedTokens,
        UsageLimit=UsageLimit,
        UsageLimitContinuationDecision=UsageLimitContinuationDecision,
        decide_usage_limit_continuation=decide_usage_limit_continuation,
        iter_stage_chain=iter_stage_chain,
        __all__=runtime_all,
    )

    assert runtime.AgentService is AgentService
    assert runtime.AssistantTurn is AssistantTurn
    assert runtime.ChainEntry is ChainEntry
    assert runtime.ConfiguredCandidateSelection is ConfiguredCandidateSelection
    assert runtime.ContinueNow is ContinueNow
    assert runtime.CredentialFailure is CredentialFailure
    assert runtime.HardError is HardError
    assert runtime.iter_stage_chain is iter_stage_chain
    assert runtime.ParsedTurn == ParsedTurn
    assert runtime.PromptTokens is PromptTokens
    assert runtime.ProviderSessionState is ProviderSessionState
    assert runtime.ProviderSessionStateRequest is ProviderSessionStateRequest
    assert runtime.Result is Result
    assert runtime.RunKind is RunKind
    assert runtime.ServiceRegistry is ServiceRegistry
    assert runtime.SleepUntil is SleepUntil
    assert runtime.Stop is Stop
    assert runtime.StageOverride is StageOverride
    assert runtime.TransientError is TransientError
    assert runtime.UnsupportedTokens is UnsupportedTokens
    assert runtime.UsageLimitContinuationDecision is UsageLimitContinuationDecision
    assert runtime.PermanentlyExhausted is PermanentlyExhausted
    assert runtime.TemporaryUsageLimit is TemporaryUsageLimit
    assert runtime.UsageLimit is UsageLimit
    assert "AgentRunner" not in runtime.__all__
    assert "AgentRunnerProtocol" not in runtime.__all__
    assert "RunRequest" not in runtime.__all__
    assert "run" not in runtime.__all__
    assert "OneShotRunRequest" in runtime.__all__
    assert "OneShotRunResult" in runtime.__all__
    assert "OneShotRuntime" in runtime.__all__
    assert "OneShotRuntimeExecutionAdapter" in runtime.__all__
    assert "OneShotRuntimeMetadata" in runtime.__all__
    assert "PromptRunRequest" in runtime.__all__
    assert "PromptRunSession" in runtime.__all__
    assert "PromptRuntime" in runtime.__all__
    assert "PromptRuntimeExecutionAdapter" in runtime.__all__
    assert "WorktreeMount" in runtime.__all__
    assert "run_one_shot" in runtime.__all__
    assert "run_prompt" in runtime.__all__
    assert runtime.decide_usage_limit_continuation is decide_usage_limit_continuation


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
