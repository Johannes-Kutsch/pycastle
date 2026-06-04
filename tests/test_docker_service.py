import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pycastle.errors import DockerBuildError, DockerServiceError, PycastleError
from pycastle.services import DockerService
from pycastle.services.docker_service import BuildOutcome


# ── Helpers: streaming mode ───────────────────────────────────────────────────

_BUILDKIT_ALL_CACHED = [
    "#1 [1/2] FROM python:3.12\n",
    "#1 CACHED\n",
    "#2 [2/2] RUN pip install requests\n",
    "#2 CACHED\n",
]

_BUILDKIT_WITH_REBUILD = [
    "#1 [1/2] FROM python:3.12\n",
    "#1 CACHED\n",
    "#2 [2/2] COPY . .\n",
    "#2 DONE 2.5s\n",
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

_CLASSIC_MIXED = [
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


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_docker_service_error_is_pycastle_error():
    assert issubclass(DockerServiceError, PycastleError)


def test_docker_build_error_is_docker_service_error():
    assert issubclass(DockerBuildError, DockerServiceError)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok_result():
    r = MagicMock()
    r.returncode = 0
    return r


def _fail_result(returncode: int = 1):
    r = MagicMock()
    r.returncode = returncode
    return r


# ── build_image: output streaming ────────────────────────────────────────────


def test_build_image_streams_output_to_terminal(tmp_path):
    """build_image must not capture docker output — stdout/stderr must inherit the terminal."""
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    _, kwargs = mock_run.call_args
    assert not kwargs.get("capture_output", False)
    assert kwargs.get("stdout") is None
    assert kwargs.get("stderr") is None


# ── build_image: success path ─────────────────────────────────────────────────


def test_build_image_calls_docker_build(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("myimage", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    assert args[0] == "docker"
    assert args[1] == "build"


def test_build_image_includes_image_name(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("myimage:latest", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    idx = args.index("-t")
    assert args[idx + 1] == "myimage:latest"


def test_build_image_includes_dockerfile_path(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", dockerfile, tmp_path)
    args = mock_run.call_args[0][0]
    idx = args.index("-f")
    assert args[idx + 1] == str(dockerfile)


def test_build_image_includes_context_dir(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    assert str(tmp_path) in args


def test_build_image_returns_none_on_success(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ):
        result = DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    assert result is None


# ── build_image: no_cache flag ────────────────────────────────────────────────


def test_build_image_no_cache_adds_flag(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, no_cache=True
        )
    args = mock_run.call_args[0][0]
    assert "--no-cache" in args


def test_build_image_no_cache_false_omits_flag(tmp_path):
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
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    args = mock_run.call_args[0][0]
    assert "--build-arg" not in args


# ── build_image: failure paths ────────────────────────────────────────────────


def test_build_image_raises_docker_build_error_on_nonzero_exit(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run",
        return_value=_fail_result(returncode=1),
    ):
        with pytest.raises(DockerBuildError):
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)


def test_build_image_error_includes_exit_code(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run",
        return_value=_fail_result(returncode=2),
    ):
        with pytest.raises(DockerBuildError) as exc_info:
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    assert "2" in str(exc_info.value)


def test_build_image_raises_docker_service_error_when_docker_not_found(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", side_effect=FileNotFoundError
    ):
        with pytest.raises(DockerServiceError):
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)


def test_build_image_raises_docker_build_error_on_timeout(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["docker"], 60),
    ):
        with pytest.raises(DockerBuildError):
            DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)


# ── build_image: accepts Path and str arguments ───────────────────────────────


def test_build_image_timeout_is_forwarded_to_subprocess(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, timeout=30.0
        )
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") == 30.0


def test_build_image_default_timeout_is_none(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ) as mock_run:
        DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") is None


def test_build_image_accepts_string_paths(tmp_path):
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
    with pytest.raises(ValueError, match="image_name"):
        DockerService().build_image("", tmp_path / "Dockerfile", tmp_path)


# ── build_image: streaming mode — cache-hit detection ────────────────────────


def test_streaming_buildkit_all_cached_returns_full_cache_hit(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_ALL_CACHED),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True
        )
    assert result == BuildOutcome.FULL_CACHE_HIT


def test_streaming_buildkit_with_rebuild_returns_rebuilt(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_WITH_REBUILD),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True
        )
    assert result == BuildOutcome.REBUILT


def test_streaming_classic_all_cached_returns_full_cache_hit(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_CLASSIC_ALL_CACHED),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True
        )
    assert result == BuildOutcome.FULL_CACHE_HIT


def test_streaming_classic_mixed_returns_rebuilt(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_CLASSIC_MIXED),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True
        )
    assert result == BuildOutcome.REBUILT


def test_streaming_empty_success_output_returns_rebuilt(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen([]),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True
        )
    assert result == BuildOutcome.REBUILT


def test_default_non_streaming_path_returns_none(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.run", return_value=_ok_result()
    ):
        result = DockerService().build_image("img", tmp_path / "Dockerfile", tmp_path)
    assert result is None


def test_streaming_raises_docker_build_error_on_nonzero_exit(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_ALL_CACHED, returncode=1),
    ):
        with pytest.raises(DockerBuildError, match="exit 1"):
            DockerService().build_image(
                "img", tmp_path / "Dockerfile", tmp_path, stream=True
            )


def test_streaming_raises_docker_service_error_when_docker_not_found(tmp_path):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        side_effect=FileNotFoundError,
    ):
        with pytest.raises(DockerServiceError):
            DockerService().build_image(
                "img", tmp_path / "Dockerfile", tmp_path, stream=True
            )


def test_streaming_raises_docker_build_error_on_timeout(tmp_path):
    mock_proc = _mock_popen(_BUILDKIT_ALL_CACHED)
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(["docker"], 60)
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=mock_proc,
    ):
        with pytest.raises(DockerBuildError, match="timed out"):
            DockerService().build_image(
                "img", tmp_path / "Dockerfile", tmp_path, stream=True, timeout=60.0
            )


def test_streaming_pipes_output_to_terminal(tmp_path, capsys):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_ALL_CACHED),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True
        )
    captured = capsys.readouterr()
    assert "CACHED" in captured.out


def test_streaming_rebuild_start_callback_fires_once_on_first_executed_buildkit_layer(
    tmp_path,
):
    callback = MagicMock()
    buildkit_output = [
        "#1 [internal] load .dockerignore\n",
        "#1 DONE 0.1s\n",
        "#2 [1/3] FROM python:3.12\n",
        "#2 CACHED\n",
        "#3 [2/3] RUN apt-get install -y git\n",
        "#3 DONE 4.2s\n",
        "#4 [3/3] COPY . .\n",
        "#4 DONE 0.5s\n",
    ]

    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(buildkit_output),
    ):
        result = DockerService().build_image(
            "img",
            tmp_path / "Dockerfile",
            tmp_path,
            stream=True,
            on_rebuild_start=callback,
        )

    assert result == BuildOutcome.REBUILT
    callback.assert_called_once_with()


def test_streaming_rebuild_start_callback_not_called_for_all_cached_classic_output(
    tmp_path,
):
    callback = MagicMock()

    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_CLASSIC_ALL_CACHED),
    ):
        result = DockerService().build_image(
            "img",
            tmp_path / "Dockerfile",
            tmp_path,
            stream=True,
            on_rebuild_start=callback,
        )

    assert result == BuildOutcome.FULL_CACHE_HIT
    callback.assert_not_called()


def test_streaming_rebuild_start_callback_waits_through_blank_line_in_classic_output(
    tmp_path,
):
    callback = MagicMock()
    classic_output = [
        "Step 1/2 : FROM python:3.12\n",
        " ---> Using cache\n",
        " ---> abc123\n",
        "Step 2/2 : COPY . .\n",
        "\n",
        " ---> Running in 789abc\n",
        "Successfully built 789abc\n",
    ]

    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(classic_output),
    ):
        result = DockerService().build_image(
            "img",
            tmp_path / "Dockerfile",
            tmp_path,
            stream=True,
            on_rebuild_start=callback,
        )

    assert result == BuildOutcome.REBUILT
    callback.assert_called_once_with()


# ── build_image: terse mode — full cache hit ──────────────────────────────────


def test_terse_full_cache_hit_prints_up_to_date_summary(tmp_path, capsys):
    """Terse mode: full cache hit produces a single terminal summary line."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_ALL_CACHED),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert result == BuildOutcome.FULL_CACHE_HIT
    assert "Building Docker Image · up to date" in captured.out
    assert "CACHED" not in captured.out
    assert "#1" not in captured.out


# ── build_image: terse mode — rebuilt ────────────────────────────────────────

_BUILDKIT_WITH_EXPORT = [
    "#1 [internal] load .dockerignore\n",
    "#1 DONE 0.1s\n",
    "#2 [1/3] FROM python:3.12\n",
    "#2 CACHED\n",
    "#3 [2/3] RUN apt-get install -y git\n",
    "#3 DONE 4.2s\n",
    "#4 [3/3] COPY . .\n",
    "#4 DONE 0.5s\n",
    "#5 exporting to image\n",
    "#5 DONE 0.3s\n",
]


def test_terse_rebuilt_prints_completed_summary(tmp_path, capsys):
    """Terse mode: rebuild completes with 'completed' summary line."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_WITH_EXPORT),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert result == BuildOutcome.REBUILT
    assert "Building Docker Image · completed" in captured.out
    assert "DONE" not in captured.out


def test_terse_rebuilt_progress_passes_through_phases(tmp_path, capsys):
    """Terse mode: progress passes through preparing, step, exporting phases."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_WITH_EXPORT),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert "preparing" in captured.out
    assert "Step" in captured.out
    assert "exporting" in captured.out


def test_terse_step_counter_advances_correctly(tmp_path, capsys):
    """Step counter advances Y while keeping X fixed from first step line."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_WITH_EXPORT),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert "Step 1/3" in captured.out
    assert "Step 2/3" in captured.out
    assert "Step 3/3" in captured.out


def test_terse_classic_step_parsing(tmp_path, capsys):
    """Classic builder step lines advance the step counter."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_CLASSIC_MIXED),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert "Step 1/2" in captured.out
    assert "Step 2/2" in captured.out
    assert "Step 1/2 : FROM" not in captured.out


def test_terse_classic_full_cache_hit(tmp_path, capsys):
    """Classic builder all-cached returns FULL_CACHE_HIT with 'up to date' line."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_CLASSIC_ALL_CACHED),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert result == BuildOutcome.FULL_CACHE_HIT
    assert "Building Docker Image · up to date" in captured.out


def test_terse_empty_success_output_returns_rebuilt(tmp_path, capsys):
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen([]),
    ):
        result = DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert result == BuildOutcome.REBUILT
    assert "Building Docker Image · completed" in captured.out


# ── build_image: terse mode — non-TTY rendering ───────────────────────────────


def test_terse_non_tty_each_state_on_own_line(tmp_path, capsys):
    """Non-TTY: each state transition produces its own line, no carriage returns."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_WITH_EXPORT),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )
    captured = capsys.readouterr()
    assert "\r" not in captured.out
    lines = captured.out.splitlines()
    assert any("preparing" in ln for ln in lines)
    assert any("completed" in ln for ln in lines)


# ── build_image: terse mode — TTY rendering ───────────────────────────────────


def test_terse_tty_uses_carriage_return_between_transitions(tmp_path):
    """TTY: transitions use carriage return to rewrite in place."""
    import io

    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[method-assign]

    with (
        patch(
            "pycastle.services.docker_service.subprocess.Popen",
            return_value=_mock_popen(_BUILDKIT_WITH_EXPORT),
        ),
        patch("pycastle.services.docker_service.sys.stdout", buf),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )

    output = buf.getvalue()
    assert "\r" in output
    assert "Building Docker Image · completed" in output


def test_terse_tty_final_line_ends_with_newline(tmp_path):
    """TTY: the final summary line ends with exactly one newline."""
    import io

    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[method-assign]

    with (
        patch(
            "pycastle.services.docker_service.subprocess.Popen",
            return_value=_mock_popen(_BUILDKIT_ALL_CACHED),
        ),
        patch("pycastle.services.docker_service.sys.stdout", buf),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
        )

    output = buf.getvalue()
    assert output.endswith("\n")
    assert output.count("\n") == 1


# ── build_image: terse mode — failure ────────────────────────────────────────


def test_terse_failure_prints_failed_summary(tmp_path, capsys):
    """Terse mode: non-zero exit prints 'failed' line then dumps docker output."""
    docker_output = ["#1 [1/2] FROM python:3.12\n", "#1 DONE 1.0s\n", "ERROR: oops\n"]
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(docker_output, returncode=1),
    ):
        with pytest.raises(DockerBuildError):
            DockerService().build_image(
                "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
            )
    captured = capsys.readouterr()
    assert "Building Docker Image · failed" in captured.out
    assert "ERROR: oops" in captured.out
    assert "#1 [1/2]" in captured.out


def test_terse_failure_dumps_all_captured_docker_output(tmp_path, capsys):
    """Failure case: every docker line is dumped verbatim in original order."""
    docker_output = [f"line{i}\n" for i in range(10)]
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(docker_output, returncode=1),
    ):
        with pytest.raises(DockerBuildError):
            DockerService().build_image(
                "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
            )
    captured = capsys.readouterr()
    for i in range(10):
        assert f"line{i}" in captured.out


def test_terse_tty_failure_clears_progress_before_failed_line(tmp_path):
    """TTY failure: progress line is cleared before 'failed' line prints."""
    import io

    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[method-assign]

    docker_output = ["#1 [1/2] FROM python:3.12\n", "#1 DONE 1.0s\n"]
    with (
        patch(
            "pycastle.services.docker_service.subprocess.Popen",
            return_value=_mock_popen(docker_output, returncode=1),
        ),
        patch("pycastle.services.docker_service.sys.stdout", buf),
    ):
        with pytest.raises(DockerBuildError):
            DockerService().build_image(
                "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
            )

    output = buf.getvalue()
    # The clear sequence (\r\x1b[K) must appear before the "failed" line
    failed_pos = output.find("Building Docker Image · failed")
    clear_pos = output.rfind("\r\x1b[K", 0, failed_pos)
    assert clear_pos != -1, "Expected clear sequence before 'failed' line"


# ── build_image: terse mode — docker not found ───────────────────────────────


def test_terse_raises_docker_service_error_when_docker_not_found(tmp_path):
    """Terse mode: FileNotFoundError from Popen raises DockerServiceError."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        side_effect=FileNotFoundError,
    ):
        with pytest.raises(DockerServiceError):
            DockerService().build_image(
                "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=True
            )


def test_terse_raises_docker_build_error_on_timeout(tmp_path):
    """Terse mode: TimeoutExpired from proc.wait raises DockerBuildError."""
    mock_proc = _mock_popen(_BUILDKIT_ALL_CACHED)
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(["docker"], 60)
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=mock_proc,
    ):
        with pytest.raises(DockerBuildError, match="timed out"):
            DockerService().build_image(
                "img",
                tmp_path / "Dockerfile",
                tmp_path,
                stream=True,
                terse=True,
                timeout=60.0,
            )


# ── build_image: verbose stream unaffected by terse ──────────────────────────


def test_verbose_stream_still_pipes_raw_output(tmp_path, capsys):
    """stream=True without terse still pipes raw docker output."""
    with patch(
        "pycastle.services.docker_service.subprocess.Popen",
        return_value=_mock_popen(_BUILDKIT_ALL_CACHED),
    ):
        DockerService().build_image(
            "img", tmp_path / "Dockerfile", tmp_path, stream=True, terse=False
        )
    captured = capsys.readouterr()
    assert "CACHED" in captured.out
    assert "Building Docker Image" not in captured.out
