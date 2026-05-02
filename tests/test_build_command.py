from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
from pycastle.services.docker_service import DockerService
from pycastle.errors import DockerBuildError, DockerServiceError

_cfg = Config(docker_image_name="test-image")


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
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        with pytest.raises(SystemExit):
            main(no_cache=True, docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "--no-cache" in cmd


def test_main_omits_no_cache_flag_by_default(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        with pytest.raises(SystemExit):
            main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "--no-cache" not in cmd


# ── python_version extraction ─────────────────────────────────────────────────


def test_main_passes_python_version_from_file(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".python-version").write_text("3.12.1\n")
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        with pytest.raises(SystemExit):
            main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "PYTHON_VERSION=3.12" in cmd


def test_main_python_version_short_form_unchanged(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".python-version").write_text("3.12\n")
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        with pytest.raises(SystemExit):
            main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "PYTHON_VERSION=3.12" in cmd


def test_main_python_version_single_segment_unchanged(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".python-version").write_text("3\n")
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        with pytest.raises(SystemExit):
            main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "PYTHON_VERSION=3" in cmd


def test_main_no_python_version_when_file_absent(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = DockerService()
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_subprocess_ok()
    ) as mock_run:
        with pytest.raises(SystemExit):
            main(docker_service=svc, cfg=_cfg)
    cmd = mock_run.call_args[0][0]
    assert "--build-arg" not in cmd


# ── exit codes ────────────────────────────────────────────────────────────────


def test_main_exits_zero_on_success(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service()
    with pytest.raises(SystemExit) as exc_info:
        main(docker_service=svc, cfg=_cfg)
    assert exc_info.value.code == 0


def test_main_exits_one_on_docker_service_error(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service(side_effect=DockerServiceError("docker not found"))
    with pytest.raises(SystemExit) as exc_info:
        main(docker_service=svc)
    assert exc_info.value.code == 1


def test_main_exits_one_on_docker_build_error(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service(side_effect=DockerBuildError("build failed"))
    with pytest.raises(SystemExit) as exc_info:
        main(docker_service=svc)
    assert exc_info.value.code == 1


def test_main_prints_error_message_to_stderr(tmp_path, monkeypatch, capsys):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service(side_effect=DockerServiceError("docker not found"))
    with pytest.raises(SystemExit):
        main(docker_service=svc, cfg=_cfg)
    assert "docker not found" in capsys.readouterr().err


# ── default DockerService is created when none provided ──────────────────────


def test_main_creates_default_docker_service(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    with patch("pycastle.build_command.DockerService") as mock_cls:
        instance = _make_docker_service()
        mock_cls.return_value = instance
        with pytest.raises(SystemExit):
            main(cfg=_cfg)
    mock_cls.assert_called_once_with()


# ── Issue 203: cfg injection into build_command.main ─────────────────────────


def test_build_command_uses_docker_image_name_from_cfg(tmp_path, monkeypatch):
    """main(cfg=Config(docker_image_name='myimg', ...)) must pass 'myimg' to build_image."""
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build_image.return_value = None

    with pytest.raises(SystemExit):
        main(
            docker_service=svc,
            cfg=Config(docker_image_name="myimg", dockerfile=Path("Dockerfile")),
        )

    assert svc.build_image.call_args[0][0] == "myimg"


def test_build_command_uses_dockerfile_from_cfg(tmp_path, monkeypatch):
    """main(cfg=Config(..., dockerfile=Path('custom/Df'))) must pass that path to build_image."""
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()
    svc.build_image.return_value = None
    custom_df = Path("custom/Dockerfile")

    with pytest.raises(SystemExit):
        main(
            docker_service=svc,
            cfg=Config(docker_image_name="img", dockerfile=custom_df),
        )

    assert svc.build_image.call_args[0][1] == custom_df


# ── Issue 222: empty docker_image_name guard ──────────────────────────────────


def test_build_command_exits_one_when_docker_image_name_is_empty(tmp_path, monkeypatch):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()

    with pytest.raises(SystemExit) as exc_info:
        main(
            docker_service=svc,
            cfg=Config(docker_image_name="", dockerfile=Path("Dockerfile")),
        )

    assert exc_info.value.code == 1


def test_build_command_empty_docker_image_name_prints_helpful_message(
    tmp_path, monkeypatch, capsys
):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()

    with pytest.raises(SystemExit):
        main(
            docker_service=svc,
            cfg=Config(docker_image_name="", dockerfile=Path("Dockerfile")),
        )

    err = capsys.readouterr().err
    assert "docker_image_name" in err
    assert "pycastle init" in err


def test_build_command_empty_docker_image_name_does_not_call_docker(
    tmp_path, monkeypatch
):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = MagicMock()

    with pytest.raises(SystemExit):
        main(
            docker_service=svc,
            cfg=Config(docker_image_name="", dockerfile=Path("Dockerfile")),
        )

    svc.build_image.assert_not_called()


# ── Issue 223: success message on build ──────────────────────────────────────


def test_main_prints_success_message_to_stdout_on_success(
    tmp_path, monkeypatch, capsys
):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service()
    with pytest.raises(SystemExit):
        main(docker_service=svc, cfg=_cfg)
    out = capsys.readouterr().out
    assert "Build complete" in out


def test_main_does_not_print_success_message_on_failure(tmp_path, monkeypatch, capsys):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service(side_effect=DockerServiceError("build failed"))
    with pytest.raises(SystemExit):
        main(docker_service=svc)
    out = capsys.readouterr().out
    assert "Build complete" not in out


def test_main_success_message_not_on_stderr(tmp_path, monkeypatch, capsys):
    from pycastle.build_command import main

    monkeypatch.chdir(tmp_path)
    svc = _make_docker_service()
    with pytest.raises(SystemExit):
        main(docker_service=svc)
    err = capsys.readouterr().err
    assert "Build complete" not in err
