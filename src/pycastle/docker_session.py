import io
import re
import sys
import tarfile
import threading
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import cast

import docker
from docker.models.containers import Container as DockerContainer

from .config import Config
from .errors import DockerError, DockerTimeoutError
from .worktree import CONTAINER_PARENT_GIT, patch_gitdir_for_container


def _parse_parent_git(git_file: Path) -> Path | None:
    m = re.search(r"gitdir:\s*(.+)", git_file.read_text(encoding="utf-8"))
    if not m:
        return None
    gitdir = m.group(1).strip().replace("\\", "/")
    idx = gitdir.find(".git/worktrees/")
    if idx == -1:
        return None
    return Path(gitdir[:idx] + ".git")


def build_volume_spec(
    mount_path: Path,
    worktree_host_path: Path | None = None,
    gitdir_overlay: Path | None = None,
) -> tuple[dict, Path | None]:
    """Compute the Docker volume specification from host paths.

    Returns (volumes_dict, auto_overlay) where auto_overlay is a host path
    that DockerSession.__exit__ must delete, or None if no overlay was created.
    """
    repo_path = str(mount_path.resolve()).replace("\\", "/")

    if worktree_host_path is not None:
        wt_path = str(worktree_host_path.resolve()).replace("\\", "/")
        parent_git_path = str((mount_path / ".git").resolve()).replace("\\", "/")
        volumes: dict = {
            wt_path: {"bind": "/home/agent/workspace", "mode": "rw"},
            repo_path: {"bind": "/home/agent/repo", "mode": "ro"},
            parent_git_path: {"bind": CONTAINER_PARENT_GIT, "mode": "rw"},
        }
        if gitdir_overlay is not None:
            overlay_path = str(gitdir_overlay.resolve()).replace("\\", "/")
            volumes[overlay_path] = {"bind": "/home/agent/workspace/.git", "mode": "ro"}
        return volumes, None

    git_file = mount_path / ".git"
    if git_file.is_file():
        overlay = patch_gitdir_for_container(mount_path)
        parent_git = _parse_parent_git(git_file)
        if parent_git is not None and parent_git.exists():
            parent_git_str = str(parent_git.resolve()).replace("\\", "/")
            volumes = {
                repo_path: {"bind": "/home/agent/workspace", "mode": "rw"},
                parent_git_str: {"bind": CONTAINER_PARENT_GIT, "mode": "rw"},
            }
            if overlay is not None:
                overlay_path = str(overlay.resolve()).replace("\\", "/")
                volumes[overlay_path] = {
                    "bind": "/home/agent/workspace/.git",
                    "mode": "ro",
                }
                return volumes, overlay
            return volumes, None
        if overlay is not None:
            overlay.unlink(missing_ok=True)

    return {repo_path: {"bind": "/home/agent/workspace", "mode": "rw"}}, None


class DockerSession:
    def __init__(
        self,
        volumes: dict,
        container_env: dict[str, str],
        image_name: str,
        cfg: Config,
        docker_client=None,
        auto_overlay: Path | None = None,
    ) -> None:
        self._volumes = volumes
        self._container_env = container_env
        self._image_name = image_name
        self._cfg = cfg
        self._auto_overlay = auto_overlay
        self._owns_client = docker_client is None
        self._client = docker_client if docker_client is not None else docker.from_env()
        self._container: DockerContainer | None = None

    @property
    def _active_container(self) -> DockerContainer:
        if self._container is None:
            raise DockerError("Container not started")
        return self._container

    def __enter__(self) -> "DockerSession":
        self._container = self._client.containers.run(
            self._image_name,
            detach=True,
            volumes=self._volumes,
            environment=self._container_env,
            working_dir="/home/agent/workspace",
        )
        return self

    def __exit__(self, *_) -> None:
        if self._auto_overlay is not None:
            try:
                self._auto_overlay.unlink(missing_ok=True)
            except Exception:
                pass
            self._auto_overlay = None
        if self._container is not None:
            try:
                self._container.stop(timeout=5)
            except Exception:
                pass
            try:
                self._container.remove(force=True)
            except Exception:
                pass
            self._container = None
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass

    def exec_simple(self, command: str, timeout: float | None = None) -> str:
        container = self._active_container
        result_holder: list = [None]
        exc_holder: list = [None]

        def _run() -> None:
            try:
                result_holder[0] = container.exec_run(
                    ["bash", "-c", command],
                    demux=True,
                    workdir="/home/agent/workspace",
                    environment=self._container_env,
                )
            except Exception as exc:
                exc_holder[0] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            raise DockerTimeoutError(f"Command timed out after {timeout}s: {command}")

        if exc_holder[0] is not None:
            raise exc_holder[0]

        result = result_holder[0]
        stdout = (result.output[0] or b"").decode("utf-8", errors="replace")
        stderr = (result.output[1] or b"").decode("utf-8", errors="replace")
        if result.exit_code != 0:
            raise DockerError(
                f"Command failed (exit {result.exit_code}): {stderr.strip() or stdout.strip()}"
            )
        if stderr and not stdout:
            print(f"  [exec stderr] {stderr.strip()}", file=sys.stderr)
        return stdout

    def exec_stream(self, command: str) -> Iterator[bytes]:
        result = self._active_container.exec_run(
            ["bash", "-c", command],
            stream=True,
            workdir="/home/agent/workspace",
        )
        return cast(Iterator[bytes], result.output)

    def write_file(self, content: str, container_path: str) -> None:
        data = content.encode("utf-8")
        path = PurePosixPath(container_path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=path.name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        self._active_container.put_archive(str(path.parent), buf)
