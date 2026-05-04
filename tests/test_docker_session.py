import tarfile
import threading
from pathlib import Path, PureWindowsPath
from unittest.mock import MagicMock, patch

import pytest

from pycastle.config import Config
from pycastle.docker_session import DockerSession, build_volume_spec
from pycastle.errors import DockerError, DockerTimeoutError
from pycastle.worktree import CONTAINER_PARENT_GIT


# ── Plain repo case ───────────────────────────────────────────────────────────


def test_plain_repo_mounts_mount_path_rw_at_workspace(tmp_path):
    """Plain repo (.git is a directory): single RW mount at /home/agent/workspace."""
    (tmp_path / ".git").mkdir()

    volumes, auto_overlay = build_volume_spec(tmp_path)

    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace" in bound_paths
    assert bound_paths["/home/agent/workspace"] == str(tmp_path.resolve()).replace(
        "\\", "/"
    )
    assert volumes[bound_paths["/home/agent/workspace"]]["mode"] == "rw"


def test_plain_repo_auto_overlay_is_none(tmp_path):
    """Plain repo: no overlay file is created, auto_overlay is None."""
    (tmp_path / ".git").mkdir()

    _, auto_overlay = build_volume_spec(tmp_path)

    assert auto_overlay is None


def test_plain_repo_has_single_volume(tmp_path):
    """Plain repo: only one volume mount is produced."""
    (tmp_path / ".git").mkdir()

    volumes, _ = build_volume_spec(tmp_path)

    assert len(volumes) == 1


# ── Explicit worktree case ────────────────────────────────────────────────────


def test_explicit_worktree_mounts_worktree_rw_at_workspace(tmp_path):
    """Explicit worktree: worktree_host_path is bound RW at /home/agent/workspace."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    volumes, _ = build_volume_spec(tmp_path, worktree_host_path=worktree)

    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace" in bound_paths
    assert bound_paths["/home/agent/workspace"] == str(worktree.resolve()).replace(
        "\\", "/"
    )
    assert volumes[bound_paths["/home/agent/workspace"]]["mode"] == "rw"


def test_explicit_worktree_mounts_host_repo_ro_at_repo(tmp_path):
    """Explicit worktree: mount_path is bound RO at /home/agent/repo."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    volumes, _ = build_volume_spec(tmp_path, worktree_host_path=worktree)

    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/repo" in bound_paths
    assert bound_paths["/home/agent/repo"] == str(tmp_path.resolve()).replace("\\", "/")
    assert volumes[bound_paths["/home/agent/repo"]]["mode"] == "ro"


def test_explicit_worktree_mounts_parent_git_rw_at_container_git(tmp_path):
    """Explicit worktree: mount_path/.git is bound RW at CONTAINER_PARENT_GIT."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    volumes, _ = build_volume_spec(tmp_path, worktree_host_path=worktree)

    expected_host = str((tmp_path / ".git").resolve()).replace("\\", "/")
    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert CONTAINER_PARENT_GIT in bound_paths
    assert bound_paths[CONTAINER_PARENT_GIT] == expected_host
    assert volumes[bound_paths[CONTAINER_PARENT_GIT]]["mode"] == "rw"


def test_explicit_worktree_auto_overlay_is_none(tmp_path):
    """Explicit worktree without gitdir_overlay: auto_overlay is None."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    _, auto_overlay = build_volume_spec(tmp_path, worktree_host_path=worktree)

    assert auto_overlay is None


def test_explicit_worktree_with_gitdir_overlay_mounts_it_at_workspace_git(tmp_path):
    """Explicit worktree with gitdir_overlay: overlay is bound RO at /home/agent/workspace/.git."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    overlay = tmp_path / "overlay.gitdir"
    overlay.write_text("gitdir: /.pycastle-parent-git/worktrees/my-branch\n")

    volumes, auto_overlay = build_volume_spec(
        tmp_path, worktree_host_path=worktree, gitdir_overlay=overlay
    )

    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace/.git" in bound_paths
    assert bound_paths["/home/agent/workspace/.git"] == str(overlay.resolve()).replace(
        "\\", "/"
    )
    assert volumes[bound_paths["/home/agent/workspace/.git"]]["mode"] == "ro"
    assert auto_overlay is None


def test_explicit_worktree_without_overlay_has_three_volumes(tmp_path):
    """Explicit worktree without overlay: exactly three volume mounts."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    volumes, _ = build_volume_spec(tmp_path, worktree_host_path=worktree)

    assert len(volumes) == 3


def test_explicit_worktree_with_overlay_has_four_volumes(tmp_path):
    """Explicit worktree with overlay: exactly four volume mounts."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    overlay = tmp_path / "overlay.gitdir"
    overlay.write_text("gitdir: /.pycastle-parent-git/worktrees/my-branch\n")

    volumes, _ = build_volume_spec(
        tmp_path, worktree_host_path=worktree, gitdir_overlay=overlay
    )

    assert len(volumes) == 4


# ── Implicit worktree case ────────────────────────────────────────────────────


def _make_implicit_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a tmp_path with a .git file pointing to a parent git dir."""
    parent = tmp_path / "parent_repo"
    parent_git = parent / ".git"
    parent_git.mkdir(parents=True)
    worktree_name = "my-branch"
    (parent_git / "worktrees").mkdir()
    (parent_git / "worktrees" / worktree_name).mkdir()

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_file = worktree / ".git"
    git_file.write_text(
        f"gitdir: {parent_git}/worktrees/{worktree_name}\n", encoding="utf-8"
    )
    return worktree, parent_git


def test_implicit_worktree_mounts_mount_path_rw_at_workspace(tmp_path):
    """Implicit worktree (.git is a file): mount_path bound RW at /home/agent/workspace."""
    worktree, _ = _make_implicit_worktree(tmp_path)

    volumes, auto_overlay = build_volume_spec(worktree)

    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace" in bound_paths
    assert bound_paths["/home/agent/workspace"] == str(worktree.resolve()).replace(
        "\\", "/"
    )
    assert volumes[bound_paths["/home/agent/workspace"]]["mode"] == "rw"


def test_implicit_worktree_mounts_parent_git_rw_at_container_git(tmp_path):
    """Implicit worktree: parent git dir bound RW at CONTAINER_PARENT_GIT."""
    worktree, parent_git = _make_implicit_worktree(tmp_path)

    volumes, _ = build_volume_spec(worktree)

    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert CONTAINER_PARENT_GIT in bound_paths
    assert bound_paths[CONTAINER_PARENT_GIT] == str(parent_git.resolve()).replace(
        "\\", "/"
    )
    assert volumes[bound_paths[CONTAINER_PARENT_GIT]]["mode"] == "rw"


def test_implicit_worktree_mounts_overlay_ro_at_workspace_git(tmp_path):
    """Implicit worktree: created overlay is bound RO at /home/agent/workspace/.git."""
    worktree, _ = _make_implicit_worktree(tmp_path)

    volumes, auto_overlay = build_volume_spec(worktree)

    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace/.git" in bound_paths
    assert auto_overlay is not None
    assert bound_paths["/home/agent/workspace/.git"] == str(
        auto_overlay.resolve()
    ).replace("\\", "/")
    assert volumes[bound_paths["/home/agent/workspace/.git"]]["mode"] == "ro"


def test_implicit_worktree_returns_auto_overlay_path(tmp_path):
    """Implicit worktree: auto_overlay is a valid existing path after build_volume_spec."""
    worktree, _ = _make_implicit_worktree(tmp_path)

    _, auto_overlay = build_volume_spec(worktree)

    assert auto_overlay is not None
    assert auto_overlay.exists()


def test_implicit_worktree_overlay_content_has_container_gitdir(tmp_path):
    """Implicit worktree: overlay file content rewrites host path to container-internal path."""
    worktree, _ = _make_implicit_worktree(tmp_path)

    _, auto_overlay = build_volume_spec(worktree)

    assert auto_overlay is not None
    content = auto_overlay.read_text(encoding="utf-8")
    assert f"gitdir: {CONTAINER_PARENT_GIT}/worktrees/my-branch" in content


def test_implicit_worktree_has_three_volumes(tmp_path):
    """Implicit worktree: three volume mounts (workspace, parent git, overlay)."""
    worktree, _ = _make_implicit_worktree(tmp_path)

    volumes, _ = build_volume_spec(worktree)

    assert len(volumes) == 3


# ── Implicit worktree fallback cases ─────────────────────────────────────────


def test_git_file_without_worktree_path_falls_back_to_plain_repo(tmp_path):
    """When .git is a file but gitdir lacks .git/worktrees/, falls back to single RW mount."""
    git_file = tmp_path / ".git"
    git_file.write_text("gitdir: /not/a/worktrees/path\n", encoding="utf-8")

    volumes, auto_overlay = build_volume_spec(tmp_path)

    assert len(volumes) == 1
    assert auto_overlay is None
    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace" in bound_paths
    assert volumes[bound_paths["/home/agent/workspace"]]["mode"] == "rw"


def test_git_file_without_worktree_path_cleans_up_overlay(tmp_path):
    """When .git is a file but falls back to plain repo, orphaned overlay temp file is deleted."""
    git_file = tmp_path / ".git"
    git_file.write_text("gitdir: /not/a/worktrees/path\n", encoding="utf-8")

    fake_overlay = tmp_path / "fake_overlay"
    fake_overlay.touch()

    with patch(
        "pycastle.docker_session.patch_gitdir_for_container", return_value=fake_overlay
    ):
        build_volume_spec(tmp_path)

    assert not fake_overlay.exists()


def test_git_file_with_missing_parent_git_falls_back_to_plain_repo(tmp_path):
    """When .git is a file but parent git dir doesn't exist, falls back to single RW mount."""
    git_file = tmp_path / ".git"
    git_file.write_text(
        "gitdir: /nonexistent/.git/worktrees/branch\n", encoding="utf-8"
    )

    volumes, auto_overlay = build_volume_spec(tmp_path)

    assert len(volumes) == 1
    assert auto_overlay is None
    bound_paths = {v["bind"]: k for k, v in volumes.items()}
    assert "/home/agent/workspace" in bound_paths


def test_git_file_with_missing_parent_git_cleans_up_overlay(tmp_path):
    """When .git is a file but parent git dir doesn't exist, orphaned overlay is deleted."""
    git_file = tmp_path / ".git"
    git_file.write_text(
        "gitdir: /nonexistent/.git/worktrees/branch\n", encoding="utf-8"
    )

    fake_overlay = tmp_path / "fake_overlay"
    fake_overlay.touch()

    with patch(
        "pycastle.docker_session.patch_gitdir_for_container", return_value=fake_overlay
    ):
        build_volume_spec(tmp_path)

    assert not fake_overlay.exists()


# ── DockerSession tests ───────────────────────────────────────────────────────


def _mock_client(
    exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.exit_code = exit_code
    result.output = (stdout, stderr)
    client.containers.run.return_value.exec_run.return_value = result
    return client


def test_docker_session_enter_starts_container_with_volumes_and_env():
    """__enter__ calls containers.run with the injected volumes, env, and image name."""
    volumes = {"/host/path": {"bind": "/home/agent/workspace", "mode": "rw"}}
    env = {"FOO": "bar"}
    mock_client = MagicMock()
    session = DockerSession(
        volumes=volumes,
        container_env=env,
        image_name="test-image",
        cfg=Config(),
        docker_client=mock_client,
    )

    session.__enter__()

    mock_client.containers.run.assert_called_once_with(
        "test-image",
        detach=True,
        volumes=volumes,
        environment=env,
        working_dir="/home/agent/workspace",
    )


def test_docker_session_exec_simple_returns_stdout_on_success():
    """exec_simple returns decoded stdout when command exits with code 0."""
    mock_client = _mock_client(exit_code=0, stdout=b"hello\n")
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()

    result = session.exec_simple("echo hello")

    assert result == "hello\n"


def test_docker_session_exec_simple_raises_docker_error_on_nonzero_exit():
    """exec_simple raises DockerError when command exits with non-zero code."""
    mock_client = _mock_client(exit_code=1, stderr=b"something went wrong")
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()

    with pytest.raises(DockerError, match="something went wrong"):
        session.exec_simple("bad command")


def test_docker_session_exec_simple_raises_timeout_error():
    """exec_simple raises DockerTimeoutError when command exceeds the timeout."""
    mock_client = MagicMock()
    unblock = threading.Event()

    def blocking_exec(*args, **kwargs):
        unblock.wait()

    mock_client.containers.run.return_value.exec_run.side_effect = blocking_exec
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()

    try:
        with pytest.raises(DockerTimeoutError):
            session.exec_simple("sleep 100", timeout=0.05)
    finally:
        unblock.set()


def test_docker_session_exit_stops_and_removes_container():
    """__exit__ stops and removes the container."""
    mock_client = MagicMock()
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()
    mock_container = mock_client.containers.run.return_value

    session.__exit__(None, None, None)

    mock_container.stop.assert_called_once()
    mock_container.remove.assert_called_once()


def test_docker_session_exit_closes_owned_client():
    """__exit__ closes the Docker client when it was created internally (not injected)."""
    with patch("pycastle.docker_session.docker") as mock_docker_mod:
        session = DockerSession(
            volumes={}, container_env={}, image_name="img", cfg=Config()
        )
        session.__enter__()
        session.__exit__(None, None, None)

    mock_docker_mod.from_env.return_value.close.assert_called_once()


def test_docker_session_exit_does_not_close_injected_client():
    """__exit__ does not close the Docker client when it was injected by the caller."""
    mock_client = MagicMock()
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()
    session.__exit__(None, None, None)

    mock_client.close.assert_not_called()


def test_docker_session_exit_deletes_auto_overlay(tmp_path):
    """__exit__ deletes the auto_overlay file when one is set."""
    overlay = tmp_path / "overlay.gitdir"
    overlay.write_text("gitdir: /some/path\n")
    mock_client = MagicMock()
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
        auto_overlay=overlay,
    )
    session.__enter__()

    session.__exit__(None, None, None)

    assert not overlay.exists()


def test_docker_session_exec_simple_raises_before_enter():
    """exec_simple raises DockerError when called before __enter__."""
    mock_client = MagicMock()
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )

    with pytest.raises(DockerError, match="Container not started"):
        session.exec_simple("echo hi")


def test_docker_session_exec_simple_prints_stderr_when_no_stdout(capsys):
    """exec_simple prints stderr to sys.stderr and returns empty string when stdout is empty."""
    mock_client = _mock_client(exit_code=0, stdout=b"", stderr=b"warning: something")
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()

    result = session.exec_simple("some command")

    assert result == ""
    captured = capsys.readouterr()
    assert "warning: something" in captured.err


def test_docker_session_exec_simple_reraises_docker_api_exception():
    """exec_simple re-raises exceptions thrown by the Docker API itself."""
    mock_client = MagicMock()
    mock_client.containers.run.return_value.exec_run.side_effect = RuntimeError(
        "API down"
    )
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()

    with pytest.raises(RuntimeError, match="API down"):
        session.exec_simple("any command")


def test_docker_session_exit_before_enter_is_noop():
    """__exit__ before __enter__ completes without error."""
    mock_client = MagicMock()
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )

    session.__exit__(None, None, None)

    mock_client.containers.run.return_value.stop.assert_not_called()
    mock_client.containers.run.return_value.remove.assert_not_called()


def test_docker_session_write_file_puts_archive_with_correct_content():
    """write_file calls put_archive with a tar containing the file at the right path."""
    mock_client = MagicMock()
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()

    session.write_file("hello content", "/tmp/myfile.txt")

    mock_container = mock_client.containers.run.return_value
    mock_container.put_archive.assert_called_once()
    directory, archive_buf = mock_container.put_archive.call_args[0]
    assert directory == "/tmp"
    archive_buf.seek(0)
    with tarfile.open(fileobj=archive_buf) as tar:
        member = tar.getmembers()[0]
        assert member.name == "myfile.txt"
        content = tar.extractfile(member).read().decode("utf-8")
        assert content == "hello content"


def test_docker_session_write_file_splits_container_path_as_posix_on_windows_host():
    """Regression for #467: container paths must split as POSIX even on Windows hosts.

    Simulates a Windows host by patching the module's Path symbol with PureWindowsPath.
    With the buggy implementation, parent renders as '\\home\\agent' and Docker
    rejects the put_archive call with a 404.
    """
    mock_client = MagicMock()
    session = DockerSession(
        volumes={},
        container_env={},
        image_name="img",
        cfg=Config(),
        docker_client=mock_client,
    )
    session.__enter__()

    with patch("pycastle.docker_session.Path", PureWindowsPath):
        session.write_file("token", "/home/agent/.claude.json")

    mock_container = mock_client.containers.run.return_value
    directory, _ = mock_container.put_archive.call_args[0]
    assert "\\" not in directory
    assert directory == "/home/agent"
