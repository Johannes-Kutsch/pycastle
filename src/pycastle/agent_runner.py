import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from .agent_result import CancellationToken, PreflightFailure
from .config import Config
from .container_runner import ContainerRunner, _preflight, _prepare, _setup, _work
from .errors import AgentTimeoutError, BranchCollisionError, UsageLimitError
from .git_service import GitService
from .worktree import patch_gitdir_for_container


class AgentRunnerProtocol(Protocol):
    async def run(
        self,
        *,
        name: str,
        prompt_file: Path,
        mount_path: Path,
        prompt_args: dict[str, str] | None = None,
        branch: str | None = None,
        sha: str | None = None,
        skip_preflight: bool = False,
        model: str = "",
        effort: str = "",
        stage: str = "",
        token: CancellationToken | None = None,
        status_display=None,
    ) -> str | PreflightFailure: ...

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
    ) -> list[tuple[str, str, str]]: ...


class AgentRunner:
    def __init__(
        self,
        env: dict[str, str],
        cfg: Config,
        git_service: GitService,
        docker_client=None,
    ) -> None:
        self._env = env
        self._cfg = cfg
        self._git_service = git_service
        self._docker_client = docker_client
        self._branch_locks: dict[str, asyncio.Lock] = {}

    async def run(
        self,
        *,
        name: str,
        prompt_file: Path,
        mount_path: Path,
        prompt_args: dict[str, str] | None = None,
        branch: str | None = None,
        sha: str | None = None,
        skip_preflight: bool = False,
        model: str = "",
        effort: str = "",
        stage: str = "",
        token: CancellationToken | None = None,
        status_display=None,
    ) -> str | PreflightFailure:
        if status_display is None:
            from .iteration._deps import NullStatusDisplay

            status_display = NullStatusDisplay()

        _token = token if token is not None else CancellationToken()
        if _token.is_cancelled:
            raise UsageLimitError("Agent cancelled due to usage limit")

        lock: asyncio.Lock | None = None
        if branch:
            if branch not in self._branch_locks:
                self._branch_locks[branch] = asyncio.Lock()
            lock = self._branch_locks[branch]
            if lock.locked():
                raise BranchCollisionError(
                    f"Branch {branch!r} already has an agent running"
                )
            await lock.acquire()

        worktree_host_path: Path | None = None
        gitdir_overlay: Path | None = None
        try:
            if branch:
                m = re.search(r"issue-(\d+)", branch)
                worktree_name = (
                    f"issue-{m.group(1)}"
                    if m
                    else re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
                )
                worktree_host_path = (
                    mount_path / self._cfg.pycastle_dir / ".worktrees" / worktree_name
                )
                self._git_service.create_worktree(
                    mount_path, worktree_host_path, branch, sha
                )
                gitdir_overlay = patch_gitdir_for_container(worktree_host_path)

            loop = asyncio.get_event_loop()
            runner = ContainerRunner(
                name,
                mount_path,
                self._env,
                branch=branch,
                worktree_host_path=worktree_host_path,
                gitdir_overlay=gitdir_overlay,
                model=model,
                effort=effort,
                docker_client=self._docker_client,
                cfg=self._cfg,
            )

            @asynccontextmanager
            async def _worktree_lifecycle():
                try:
                    yield
                finally:
                    exc: BaseException | None = None
                    try:
                        runner.__exit__(None, None, None)
                    except BaseException as e:
                        exc = e
                    if worktree_host_path and not _token.wants_worktree_preserved:
                        try:
                            clean = self._git_service.is_working_tree_clean(
                                worktree_host_path
                            )
                        except Exception:
                            clean = False
                        if clean:
                            try:
                                self._git_service.remove_worktree(
                                    mount_path, worktree_host_path
                                )
                            except BaseException as e:
                                if exc is None:
                                    exc = e
                    if gitdir_overlay:
                        gitdir_overlay.unlink(missing_ok=True)
                    if exc is not None:
                        raise exc

            async with _worktree_lifecycle():
                await _setup(
                    name, runner, loop, None, self._git_service, self._cfg, status_display
                )
                await _prepare(
                    name, runner, loop, None, prompt_file, prompt_args or {}, status_display
                )
                if not skip_preflight:
                    failures = await _preflight(
                        name,
                        runner,
                        loop,
                        None,
                        list(self._cfg.preflight_checks),
                        status_display,
                    )
                    if failures:
                        return PreflightFailure(failures=tuple(failures))
                output = ""
                retries_left = self._cfg.timeout_retries
                while True:
                    try:
                        output = await _work(name, runner, loop, status_display)
                        break
                    except AgentTimeoutError:
                        if retries_left <= 0:
                            raise
                        restart_num = self._cfg.timeout_retries - retries_left + 1
                        status_display.print(
                            f"[{name}] Timeout — restarting"
                            f" (attempt {restart_num}/{self._cfg.timeout_retries})"
                        )
                        retries_left -= 1
                    except UsageLimitError:
                        _token.cancel(preserve_worktree=True)
                        raise
                return output
        finally:
            status_display.remove_agent(name)
            if lock is not None and lock.locked():
                lock.release()

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
    ) -> list[tuple[str, str, str]]:
        loop = asyncio.get_event_loop()
        runner = ContainerRunner(
            name,
            mount_path,
            self._env,
            docker_client=self._docker_client,
            cfg=self._cfg,
        )
        try:
            await _setup(name, runner, loop, None, self._git_service, self._cfg)
            return await _preflight(
                name,
                runner,
                loop,
                None,
                list(self._cfg.preflight_checks),
            )
        finally:
            try:
                runner.__exit__(None, None, None)
            except Exception:
                pass
