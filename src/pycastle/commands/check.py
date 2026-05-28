from __future__ import annotations

import asyncio
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import Config, load_config
from ..infrastructure.worktree import transient_worktree
from ..services import GitService


@dataclass
class _CheckDeps:
    repo_root: Path
    cfg: Config
    git_svc: GitService


def _run_host_check(name: str, command: str, cwd: Path) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return
    output = (result.stdout + result.stderr).strip()
    detail = f"\n{output}" if output else ""
    raise RuntimeError(f"Host check {name!r} failed: {command}{detail}")


def main(
    *,
    cfg: Config | None = None,
    git_service: GitService | None = None,
) -> None:
    resolved_cfg = cfg or load_config()
    repo_root = Path(".").resolve()
    git_svc = git_service or GitService(resolved_cfg)

    git_svc.pull_with_merge_fallback(repo_root)
    if not git_svc.is_working_tree_clean(repo_root):
        raise RuntimeError("Working tree must be clean before running host checks.")

    sha = git_svc.get_head_sha(repo_root)
    deps = _CheckDeps(repo_root=repo_root, cfg=resolved_cfg, git_svc=git_svc)

    async def _run_checks() -> None:
        async with transient_worktree(
            f"host-check-{sha[:7]}", sha=sha, deps=deps
        ) as path:
            for name, command in resolved_cfg.host_checks:
                _run_host_check(name, command, path)

    asyncio.run(_run_checks())
    print(f"Host checks passed on {platform.system()} at {sha}.")
    sys.stdout.flush()
