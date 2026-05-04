import re
from pathlib import Path

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

    if worktree_host_path:
        wt_path = str(worktree_host_path.resolve()).replace("\\", "/")
        parent_git_path = str((mount_path / ".git").resolve()).replace("\\", "/")
        volumes: dict = {
            wt_path: {"bind": "/home/agent/workspace", "mode": "rw"},
            repo_path: {"bind": "/home/agent/repo", "mode": "ro"},
            parent_git_path: {"bind": CONTAINER_PARENT_GIT, "mode": "rw"},
        }
        if gitdir_overlay:
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
            auto_overlay: Path | None = None
            if overlay is not None:
                auto_overlay = overlay
                overlay_path = str(overlay.resolve()).replace("\\", "/")
                volumes[overlay_path] = {
                    "bind": "/home/agent/workspace/.git",
                    "mode": "ro",
                }
            return volumes, auto_overlay

    return {repo_path: {"bind": "/home/agent/workspace", "mode": "rw"}}, None
