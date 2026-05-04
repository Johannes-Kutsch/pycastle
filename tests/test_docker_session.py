from pathlib import Path


from pycastle.docker_session import build_volume_spec
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
