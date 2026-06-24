import shutil
import subprocess
import sys
import tarfile
import os
import tomllib
from pathlib import Path
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


def _runtime_package_wheel(tmp_path: Path) -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    build_dir = repo_root / "build"
    runtime_build_dir = repo_root / "src/pycastle_agent_runtime/build"
    runtime_egg_info_dir = (
        repo_root / "src/pycastle_agent_runtime/pycastle_agent_runtime.egg-info"
    )
    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(runtime_build_dir, ignore_errors=True)
    shutil.rmtree(runtime_egg_info_dir, ignore_errors=True)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "./src/pycastle_agent_runtime",
                "--no-deps",
                "-w",
                str(tmp_path),
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return next(tmp_path.glob("pycastle_agent_runtime-*.whl"))
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(runtime_build_dir, ignore_errors=True)
        shutil.rmtree(runtime_egg_info_dir, ignore_errors=True)


def _runtime_package_sdist(tmp_path: Path) -> Path:
    from setuptools.build_meta import build_sdist  # type: ignore[import-untyped]

    repo_root = Path(__file__).resolve().parents[1]
    runtime_package_root = repo_root / "src/pycastle_agent_runtime"
    build_dir = repo_root / "build"
    runtime_build_dir = runtime_package_root / "build"
    runtime_egg_info_dir = runtime_package_root / "pycastle_agent_runtime.egg-info"
    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(runtime_build_dir, ignore_errors=True)
    shutil.rmtree(runtime_egg_info_dir, ignore_errors=True)
    current_dir = Path.cwd()
    try:
        os.chdir(runtime_package_root)
        sdist_name = build_sdist(str(tmp_path))
        return tmp_path / sdist_name
    finally:
        os.chdir(current_dir)
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(runtime_build_dir, ignore_errors=True)
        shutil.rmtree(runtime_egg_info_dir, ignore_errors=True)


def _runtime_python_modules_in_wheel(wheel_path: Path) -> set[str]:
    with ZipFile(wheel_path) as wheel:
        modules = {
            "pycastle_agent_runtime"
            if name == "pycastle_agent_runtime/__init__.py"
            else (
                f"pycastle_agent_runtime.{name.removeprefix('pycastle_agent_runtime/').removesuffix('/__init__.py').replace('/', '.')}"
                if name.endswith("/__init__.py")
                else f"pycastle_agent_runtime.{name.removeprefix('pycastle_agent_runtime/').removesuffix('.py').replace('/', '.')}"
            )
            for name in wheel.namelist()
            if name.startswith("pycastle_agent_runtime/") and name.endswith(".py")
        }
    return modules


def _runtime_python_modules_in_sdist(sdist_path: Path) -> set[str]:
    with tarfile.open(sdist_path, "r:gz") as sdist:
        modules = {
            "pycastle_agent_runtime"
            if Path(name).name == "__init__.py"
            else f"pycastle_agent_runtime.{Path(name).name.removesuffix('.py')}"
            for name in sdist.getnames()
            if name.count("/") == 1 and name.endswith(".py")
        }
    return modules


def _runtime_expected_module_set() -> set[str]:
    runtime_root = Path("src/pycastle_agent_runtime")
    return {
        "pycastle_agent_runtime"
        if path.name == "__init__.py"
        else f"pycastle_agent_runtime.{path.stem}"
        for path in runtime_root.glob("*.py")
    }


def _install_runtime_artifact(tmp_path: Path, artifact_kind: str) -> Path:
    artifact_path = (
        _runtime_package_wheel(tmp_path)
        if artifact_kind == "wheel"
        else _runtime_package_sdist(tmp_path)
    )
    install_dir = tmp_path / f"{artifact_kind}-site-packages"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            str(artifact_path),
            "--no-deps",
            "--target",
            str(install_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    return install_dir


def _runtime_public_api_probe(tmp_path: Path, artifact_kind: str) -> list[str]:
    install_dir = _install_runtime_artifact(tmp_path, artifact_kind)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(install_dir)!r}); "
                "import importlib.util; "
                "import pycastle_agent_runtime as runtime; "
                "from pycastle_agent_runtime import provider_session_adapter; "
                "print(importlib.util.find_spec('pycastle') is None); "
                "print(hasattr(runtime, 'PycastleError')); "
                "print(hasattr(provider_session_adapter, 'ProviderSessionPlanningRequest')); "
                "print(hasattr(provider_session_adapter, 'ProviderSessionPlanningFacts')); "
                "print(hasattr(provider_session_adapter, 'ProviderSessionAdapter')); "
            ),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    return result.stdout.splitlines()


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


def test_wheel_ships_agent_runtime_package_scaffold(tmp_path):
    wheel_members = _wheel_members(tmp_path)

    assert "pycastle_agent_runtime/__init__.py" in wheel_members
    assert "pycastle_agent_runtime/orchestration.py" not in wheel_members
    assert "pycastle_agent_runtime/py.typed" in wheel_members
    assert "pycastle_agent_runtime/pyproject.toml" in wheel_members


def test_sdist_ships_agent_runtime_package_scaffold(tmp_path):
    sdist_members = _sdist_members(tmp_path)

    assert any(
        name.endswith("/src/pycastle_agent_runtime/__init__.py")
        for name in sdist_members
    )
    assert not any(
        name.endswith("/src/pycastle_agent_runtime/orchestration.py")
        for name in sdist_members
    )
    assert any(
        name.endswith("/src/pycastle_agent_runtime/py.typed") for name in sdist_members
    )
    assert any(
        name.endswith("/src/pycastle_agent_runtime/pyproject.toml")
        for name in sdist_members
    )


def test_runtime_package_metadata_declares_standalone_distribution_path() -> None:
    metadata = tomllib.loads(
        Path("src/pycastle_agent_runtime/pyproject.toml").read_text(encoding="utf-8")
    )

    assert metadata["project"]["name"] == "pycastle-agent-runtime"
    assert metadata["tool"]["setuptools"]["packages"] == ["pycastle_agent_runtime"]
    assert metadata["tool"]["setuptools"]["package-dir"] == {
        "pycastle_agent_runtime": "."
    }
    assert metadata["tool"]["setuptools"]["package-data"] == {
        "pycastle_agent_runtime": ["py.typed", "pyproject.toml"]
    }


def test_agent_runtime_package_exports_the_runtime_surface():
    import pycastle_agent_runtime as runtime

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
    from pycastle_agent_runtime.session import (
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
    from pycastle_agent_runtime.usage_limit_decision import (
        ContinueNow,
        SleepUntil,
        Stop,
        UsageLimitContinuationDecision,
        UsageLimitOutcome,
        decide_usage_limit_continuation,
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
    assert runtime.UsageLimitOutcome is UsageLimitOutcome
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
                    "from importlib.resources import files; "
                    "print(runtime.ServiceRegistry.__module__); "
                    "print(runtime.ServiceRegistry.__name__); "
                    "print(runtime.decide_usage_limit_continuation.__module__); "
                    "print(files('pycastle_agent_runtime').joinpath('pyproject.toml').is_file())"
                ),
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.stdout.splitlines() == [
            "pycastle_agent_runtime.service_registry",
            "ServiceRegistry",
            "pycastle_agent_runtime.usage_limit_decision",
            "True",
        ]
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def test_standalone_runtime_distribution_installs_without_pycastle_package(
    tmp_path: Path,
) -> None:
    wheel_path = _runtime_package_wheel(tmp_path)
    shipped_runtime_modules = _runtime_python_modules_in_wheel(wheel_path)
    install_dir = tmp_path / "site-packages"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            str(wheel_path),
            "--no-deps",
            "--target",
            str(install_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(install_dir)!r}); "
                "import importlib; "
                "import importlib.util; "
                "from importlib.resources import files; "
                "import pycastle_agent_runtime as runtime; "
                "runtime.ServiceRegistry; "
                "runtime.ProviderSessionState; "
                "runtime.ProviderSessionStateRequest; "
                "runtime.RunKind; "
                "runtime.StageOverride; "
                "runtime.UsageLimitOutcome; "
                "runtime.decide_usage_limit_continuation; "
                "runtime.select_configured_candidate_chain; "
                "print(importlib.util.find_spec('pycastle') is None); "
                "print(runtime.ServiceRegistry.__module__); "
                "print(runtime.decide_usage_limit_continuation.__module__); "
                "print(files('pycastle_agent_runtime').joinpath('pyproject.toml').is_file()); "
                f"modules = {sorted(shipped_runtime_modules)!r}; "
                "[print(importlib.import_module(name).__name__) for name in modules]"
            ),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    stdout_lines = result.stdout.splitlines()

    assert stdout_lines[:4] == [
        "True",
        "pycastle_agent_runtime.service_registry",
        "pycastle_agent_runtime.usage_limit_decision",
        "True",
    ]
    assert set(stdout_lines[4:]) == shipped_runtime_modules


def test_standalone_runtime_wheel_ships_exact_runtime_module_set(
    tmp_path: Path,
) -> None:
    wheel_path = _runtime_package_wheel(tmp_path)

    assert (
        _runtime_python_modules_in_wheel(wheel_path) == _runtime_expected_module_set()
    )


def test_standalone_runtime_sdist_installs_without_pycastle_package(
    tmp_path: Path,
) -> None:
    sdist_path = _runtime_package_sdist(tmp_path)
    shipped_runtime_modules = _runtime_python_modules_in_sdist(sdist_path)
    install_dir = tmp_path / "site-packages"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            str(sdist_path),
            "--no-deps",
            "--target",
            str(install_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(install_dir)!r}); "
                "import importlib; "
                "import importlib.util; "
                "from importlib.resources import files; "
                "import pycastle_agent_runtime as runtime; "
                "runtime.ServiceRegistry; "
                "runtime.ProviderSessionAdapter; "
                "runtime.ProviderSessionState; "
                "runtime.ProviderSessionStateRequest; "
                "runtime.RunKind; "
                "runtime.StageOverride; "
                "runtime.UsageLimitOutcome; "
                "runtime.decide_usage_limit_continuation; "
                "runtime.select_configured_candidate_chain; "
                "print(importlib.util.find_spec('pycastle') is None); "
                "print(runtime.ServiceRegistry.__module__); "
                "print(runtime.decide_usage_limit_continuation.__module__); "
                "print(files('pycastle_agent_runtime').joinpath('pyproject.toml').is_file()); "
                f"modules = {sorted(shipped_runtime_modules)!r}; "
                "[print(importlib.import_module(name).__name__) for name in modules]"
            ),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    stdout_lines = result.stdout.splitlines()

    assert stdout_lines[:4] == [
        "True",
        "pycastle_agent_runtime.service_registry",
        "pycastle_agent_runtime.usage_limit_decision",
        "True",
    ]
    assert set(stdout_lines[4:]) == shipped_runtime_modules


def test_standalone_runtime_sdist_ships_exact_runtime_module_set(
    tmp_path: Path,
) -> None:
    sdist_path = _runtime_package_sdist(tmp_path)

    assert (
        _runtime_python_modules_in_sdist(sdist_path) == _runtime_expected_module_set()
    )


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_standalone_runtime_artifacts_expose_provider_session_adapter_contract(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    assert _runtime_public_api_probe(tmp_path, artifact_kind) == [
        "True",
        "False",
        "True",
        "True",
        "True",
    ]


def test_runtime_python_modules_in_wheel_maps_nested_package_init_to_package_name(
    tmp_path: Path,
) -> None:
    wheel_path = tmp_path / "runtime.whl"
    with ZipFile(wheel_path, "w") as wheel:
        wheel.writestr("pycastle_agent_runtime/__init__.py", "")
        wheel.writestr("pycastle_agent_runtime/provider/__init__.py", "")
        wheel.writestr("pycastle_agent_runtime/provider/session.py", "")

    assert _runtime_python_modules_in_wheel(wheel_path) == {
        "pycastle_agent_runtime",
        "pycastle_agent_runtime.provider",
        "pycastle_agent_runtime.provider.session",
    }


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
