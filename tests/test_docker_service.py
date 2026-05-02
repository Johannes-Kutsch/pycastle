import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pycastle.errors import DockerBuildError, DockerServiceError, PycastleError


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_docker_service_error_is_pycastle_error():
    assert issubclass(DockerServiceError, PycastleError)


def test_docker_build_error_is_docker_service_error():
    assert issubclass(DockerBuildError, DockerServiceError)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok_result():
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


def _fail_result(returncode: int = 1, stderr: str = "build failed"):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = ""
    r.stderr = stderr
    return r


# ── build_image: success path ─────────────────────────────────────────────────


def test_build_image_calls_docker_build(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("myimage", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    assert args[0] == "docker"
    assert args[1] == "build"


def test_build_image_includes_image_name(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("myimage:latest", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    idx = args.index("-t")
    assert args[idx + 1] == "myimage:latest"


def test_build_image_includes_dockerfile_path(tmp_path):
    from pycastle.services.docker_service import DockerService

    dockerfile = tmp_path / "Dockerfile"
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", dockerfile, tmp_path)
    args = mock_run.call_args[0][0]
    idx = args.index("-f")
    assert args[idx + 1] == str(dockerfile)


def test_build_image_includes_context_dir(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    assert str(tmp_path) in args


def test_build_image_returns_none_on_success(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ):
        result = DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    assert result is None


# ── build_image: no_cache flag ────────────────────────────────────────────────


def test_build_image_no_cache_adds_flag(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, no_cache=True
        )
    args = mock_run.call_args[0][0]
    assert "--no-cache" in args


def test_build_image_no_cache_false_omits_flag(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, no_cache=False
        )
    args = mock_run.call_args[0][0]
    assert "--no-cache" not in args


# ── build_image: python_version build arg ─────────────────────────────────────


def test_build_image_python_version_adds_build_arg(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, python_version="3.12"
        )
    args = mock_run.call_args[0][0]
    assert "--build-arg" in args
    idx = args.index("--build-arg")
    assert args[idx + 1] == "PYTHON_VERSION=3.12"


def test_build_image_no_python_version_omits_build_arg(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    assert "--build-arg" not in args


# ── build_image: failure paths ────────────────────────────────────────────────


def test_build_image_raises_docker_build_error_on_nonzero_exit(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run",
        return_value=_fail_result(returncode=1),
    ):
        with pytest.raises(DockerBuildError):
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)


def test_build_image_error_includes_stderr(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run",
        return_value=_fail_result(stderr="no space left"),
    ):
        with pytest.raises(DockerBuildError) as exc_info:
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    assert "no space left" in str(exc_info.value)


def test_build_image_raises_docker_service_error_when_docker_not_found(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", side_effect=FileNotFoundError
    ):
        with pytest.raises(DockerServiceError):
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)


def test_build_image_raises_docker_build_error_on_timeout(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["docker"], 60),
    ):
        with pytest.raises(DockerBuildError):
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)


# ── build_image: accepts Path and str arguments ───────────────────────────────


def test_build_image_timeout_is_forwarded_to_subprocess(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, timeout=30.0
        )
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") == 30.0


def test_build_image_default_timeout_is_none(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") is None


def test_build_image_accepts_string_paths(tmp_path):
    from pycastle.services.docker_service import DockerService

    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image(
            "img",
            str(tmp_path / "Dockerfile"),
            str(tmp_path),
        )
    assert mock_run.called


# ── Issue 222: empty image_name guard ────────────────────────────────────────


def test_build_image_raises_on_empty_image_name(tmp_path):
    from pycastle.services.docker_service import DockerService

    with pytest.raises(ValueError, match="image_name"):
        DockerService().build_image("", tmp_path / "Dockerfile", tmp_path)
