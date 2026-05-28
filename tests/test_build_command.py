from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
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
        svc.build_image.side_effect = side_effect
    else:
        svc.build_image.return_value = None
    return svc


def _subprocess_ok():
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    return result


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
    """main(cfg=Config(docker_image_name='myimg', ...)) must pass 'myimg' to build_image."""
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build_image.return_value = None

    main(
        docker_service=svc,
        cfg=Config(docker_image_name="myimg"),
    )

    assert svc.build_image.call_args[0][0] == "myimg"


def test_build_command_uses_default_dockerfile_path(tmp_path, monkeypatch):
    """main(cfg=Config(...)) must pass the default Dockerfile path to build_image."""
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build_image.return_value = None

    main(
        docker_service=svc,
        cfg=Config(docker_image_name="img"),
    )

    assert svc.build_image.call_args[0][1] == Path("pycastle/Dockerfile")


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

    svc.build_image.assert_not_called()


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
    """stream=True, terse=True is forwarded to docker_service.build_image."""
    from pycastle.commands.build import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build_image.return_value = None
    main(stream=True, terse=True, docker_service=svc, cfg=_cfg)
    _, kwargs = svc.build_image.call_args
    assert kwargs.get("terse") is True


def test_terse_mode_does_not_print_image_up_to_date(tmp_path, monkeypatch, capsys):
    """With terse=True the build command doesn't print 'Image up to date'."""
    from pycastle.commands.build import main
    from pycastle.services.docker_service import BuildOutcome

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build_image.return_value = BuildOutcome.FULL_CACHE_HIT
    main(stream=True, terse=True, docker_service=svc, cfg=_cfg)
    out = capsys.readouterr().out
    assert "Image up to date" not in out


def test_non_terse_stream_still_prints_image_up_to_date(tmp_path, monkeypatch, capsys):
    """stream=True without terse still prints 'Image up to date' on full cache hit."""
    from pycastle.commands.build import main
    from pycastle.services.docker_service import BuildOutcome

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build_image.return_value = BuildOutcome.FULL_CACHE_HIT
    main(stream=True, terse=False, docker_service=svc, cfg=_cfg)
    out = capsys.readouterr().out
    assert "Image up to date" in out
