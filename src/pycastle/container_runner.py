import asyncio
import io
import json
import os
import queue
import re
import subprocess
import sys
import tarfile
import threading
from contextlib import AsyncExitStack
from pathlib import Path

import docker
from docker.models.containers import Container as DockerContainer

from .config import DOCKER_IMAGE, LOGS_DIR, PYCASTLE_DIR
from .defaults.config import IDLE_TIMEOUT, PREFLIGHT_CHECKS
from .errors import (
    AgentTimeoutError,
    BranchCollisionError,
    DockerError,
    DockerTimeoutError,
    PreflightError,
)
from .worktree import (
    CONTAINER_PARENT_GIT,
    create_worktree,
    patch_gitdir_for_container,
    remove_worktree,
)

_branch_locks: dict[str, asyncio.Lock] = {}


def _format_stream_line(line: str) -> str | None:
    """Return a human-readable summary of a stream-json line, or None to suppress it."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(obj, dict):
        return line
    line_type = obj.get("type")
    if line_type == "system":
        return None
    if line_type == "result":
        result_text = obj.get("result", "")
        return result_text if result_text else None
    if line_type == "assistant":
        content = obj.get("message", {}).get("content", [])
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text)
            elif block.get("type") == "tool_use":
                parts.append(f"(tool: {block.get('name', 'unknown')})")
        return " ".join(parts) if parts else None
    return None


def _build_claude_command(model: str = "", effort: str = "") -> str:
    flags = "--print --verbose --dangerously-skip-permissions --output-format stream-json -p -"
    if model:
        flags += f" --model {model}"
    if effort:
        flags += f" --effort {effort}"
    return f"claude {flags} < /tmp/.pycastle_prompt"


class ContainerRunner:
    def __init__(
        self,
        name: str,
        mount_path: Path,
        env: dict[str, str],
        branch: str | None = None,
        worktree_host_path: Path | None = None,
        gitdir_overlay: Path | None = None,
        model: str = "",
        effort: str = "",
    ):
        self.name = name
        self.mount_path = mount_path
        self.env = env
        self.branch = branch
        self.worktree_host_path = worktree_host_path
        self.gitdir_overlay = gitdir_overlay
        self.model = model
        self.effort = effort
        self._client = docker.from_env()
        self._container: DockerContainer | None = None
        self._container_env: dict[str, str] = {}
        self._prompt: str = ""
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self._log_path = LOGS_DIR / f"{slug}.log"
        self._worktree_path = "/home/agent/workspace"

    @property
    def _active_container(self) -> DockerContainer:
        if self._container is None:
            raise DockerError("Container not started")
        return self._container

    def __enter__(self) -> "ContainerRunner":
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        repo_path = str(self.mount_path.resolve()).replace("\\", "/")

        if self.worktree_host_path:
            worktree_path = str(self.worktree_host_path.resolve()).replace("\\", "/")
            parent_git_path = str((self.mount_path / ".git").resolve()).replace(
                "\\", "/"
            )
            volumes = {
                worktree_path: {"bind": "/home/agent/workspace", "mode": "rw"},
                repo_path: {"bind": "/home/agent/repo", "mode": "ro"},
                parent_git_path: {"bind": CONTAINER_PARENT_GIT, "mode": "rw"},
            }
            if self.gitdir_overlay:
                overlay_path = str(self.gitdir_overlay.resolve()).replace("\\", "/")
                volumes[overlay_path] = {
                    "bind": "/home/agent/workspace/.git",
                    "mode": "ro",
                }
        else:
            volumes = {repo_path: {"bind": "/home/agent/workspace", "mode": "rw"}}
        working_dir = "/home/agent/workspace"

        # CLAUDE_ACCOUNT_JSON is written as a file inside the container, not passed as env var
        self._container_env = {
            k: v for k, v in self.env.items() if k != "CLAUDE_ACCOUNT_JSON"
        }

        self._container = self._client.containers.run(
            DOCKER_IMAGE,
            detach=True,
            volumes=volumes,
            environment=self._container_env,
            working_dir=working_dir,
        )

        claude_json = self.env.get("CLAUDE_ACCOUNT_JSON")
        if claude_json:
            self.write_file(claude_json, "/home/agent/.claude.json")

        return self

    def __exit__(self, *_):
        if self._container:
            try:
                self._container.stop(timeout=5)
            except Exception:
                pass
            try:
                self._container.remove(force=True)
            except Exception:
                pass

    def exec_simple(self, command: str, timeout: float | None = None) -> str:
        import threading

        container = self._active_container
        result_holder: list = [None]
        exc_holder: list = [None]

        def _run():
            try:
                result_holder[0] = container.exec_run(
                    ["bash", "-c", command],
                    demux=True,
                    workdir=self._worktree_path,
                    environment=self.env,
                )
            except Exception as exc:
                exc_holder[0] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            raise DockerTimeoutError(f"Command timed out after {timeout}s: {command}")

        if exc_holder[0]:
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

    def write_file(self, content: str, container_path: str):
        data = content.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=os.path.basename(container_path))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        self._active_container.put_archive(os.path.dirname(container_path), buf)

    def run_streaming(self) -> str:
        self.write_file(self._prompt, "/tmp/.pycastle_prompt")
        result = self._active_container.exec_run(
            ["bash", "-c", _build_claude_command(model=self.model, effort=self.effort)],
            stream=True,
            workdir=self._worktree_path,
        )

        q: queue.Queue = queue.Queue()
        _sentinel = object()

        def _feed():
            try:
                for chunk in result.output:
                    q.put(chunk)
            finally:
                q.put(_sentinel)

        threading.Thread(target=_feed, daemon=True).start()

        parts: list[str] = []
        line_buf = ""
        try:
            with open(self._log_path, "wb") as log:
                while True:
                    try:
                        chunk = q.get(timeout=IDLE_TIMEOUT)
                    except queue.Empty:
                        raise AgentTimeoutError(
                            f"Agent idle for more than {IDLE_TIMEOUT}s"
                        )
                    if chunk is _sentinel:
                        break
                    log.write(chunk)
                    log.flush()
                    text = chunk.decode("utf-8", errors="replace")
                    parts.append(text)
                    line_buf += text
                    while "\n" in line_buf:
                        line, line_buf = line_buf.split("\n", 1)
                        formatted = _format_stream_line(line)
                        if formatted is not None:
                            print(f"[{self.name}] {formatted}", flush=True)
        finally:
            try:
                self._active_container.exec_run(
                    ["bash", "-c", "rm -f /tmp/.pycastle_prompt"],
                    workdir=self._worktree_path,
                )
            except Exception:
                pass
        return "".join(parts)


async def _preflight(
    name: str,
    runner: "ContainerRunner",
    loop: asyncio.AbstractEventLoop,
    exec_timeout: float | None,
    checks: list[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    print(f"[{name}] Phase: Pre-flight")
    failures: list[tuple[str, str, str]] = []
    for check_name, command in checks:
        try:
            await loop.run_in_executor(None, runner.exec_simple, command, exec_timeout)
        except DockerError as exc:
            failures.append((check_name, command, str(exc)))
    return failures


async def _setup(
    name: str,
    runner: "ContainerRunner",
    loop: asyncio.AbstractEventLoop,
    exec_timeout: float | None,
) -> None:
    print(f"[{name}] Phase: Setup")
    await loop.run_in_executor(None, runner.__enter__)
    git_name = subprocess.check_output(
        ["git", "config", "user.name"], text=True
    ).strip()
    git_email = subprocess.check_output(
        ["git", "config", "user.email"], text=True
    ).strip()
    await loop.run_in_executor(
        None,
        runner.exec_simple,
        f"git config --global user.name '{git_name}'",
        exec_timeout,
    )
    await loop.run_in_executor(
        None,
        runner.exec_simple,
        f"git config --global user.email '{git_email}'",
        exec_timeout,
    )


async def _prepare(
    name: str,
    runner: "ContainerRunner",
    loop: asyncio.AbstractEventLoop,
    exec_timeout: float | None,
    prompt_file: Path,
    prompt_args: dict[str, str],
) -> None:
    from .prompt_pipeline import prepare_prompt

    print(f"[{name}] Phase: Prepare")
    try:
        await loop.run_in_executor(
            None,
            runner.exec_simple,
            "pip install -e '.[dev]' || pip install -r requirements.txt",
            exec_timeout,
        )
    except RuntimeError as exc:
        print(f"  [{name}] Warning: dependency install skipped: {exc}", file=sys.stderr)

    async def container_exec(cmd: str) -> str:
        return await loop.run_in_executor(None, runner.exec_simple, cmd, exec_timeout)

    prompt = await prepare_prompt(prompt_file, prompt_args, container_exec)
    runner._prompt = prompt


async def _work(
    name: str,
    runner: "ContainerRunner",
    loop: asyncio.AbstractEventLoop,
) -> str:
    print(f"[{name}] Phase: Work")
    return await loop.run_in_executor(None, runner.run_streaming)


async def run_agent(
    name: str,
    prompt_file: Path,
    mount_path: Path,
    env: dict[str, str],
    prompt_args: dict[str, str] | None = None,
    branch: str | None = None,
    exec_timeout: float | None = None,
    skip_preflight: bool = False,
    model: str = "",
    effort: str = "",
) -> str:
    print(f"\n[{name}] Started")

    lock: asyncio.Lock | None = None
    if branch:
        if branch not in _branch_locks:
            _branch_locks[branch] = asyncio.Lock()
        lock = _branch_locks[branch]
        if lock.locked():
            raise BranchCollisionError(
                f"Branch {branch!r} already has an agent running"
            )
        await lock.acquire()

    worktree_host_path: Path | None = None
    gitdir_overlay: Path | None = None
    try:
        if branch:
            branch_slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
            worktree_host_path = mount_path / PYCASTLE_DIR / ".worktrees" / branch_slug
            create_worktree(mount_path, worktree_host_path, branch)
            gitdir_overlay = patch_gitdir_for_container(worktree_host_path)

        loop = asyncio.get_event_loop()
        runner = ContainerRunner(
            name,
            mount_path,
            env,
            branch=branch,
            worktree_host_path=worktree_host_path,
            gitdir_overlay=gitdir_overlay,
            model=model,
            effort=effort,
        )
        async with AsyncExitStack() as stack:
            if gitdir_overlay:
                stack.callback(gitdir_overlay.unlink, missing_ok=True)
            if worktree_host_path:
                stack.callback(remove_worktree, mount_path, worktree_host_path)
            stack.callback(runner.__exit__, None, None, None)
            await _setup(name, runner, loop, exec_timeout)
            await _prepare(
                name, runner, loop, exec_timeout, prompt_file, prompt_args or {}
            )
            if not skip_preflight:
                failures = await _preflight(
                    name, runner, loop, exec_timeout, PREFLIGHT_CHECKS
                )
                if failures:
                    _bug_prompt = (
                        Path(__file__).parent / "defaults" / "prompts" / "bug-report.md"
                    )
                    await asyncio.gather(
                        *[
                            run_agent(
                                name=f"bug-report ({check_name})",
                                prompt_file=_bug_prompt,
                                mount_path=mount_path,
                                env=env,
                                prompt_args={
                                    "CHECK_NAME": check_name,
                                    "COMMAND": command,
                                    "OUTPUT": output,
                                },
                                exec_timeout=exec_timeout,
                                skip_preflight=True,
                            )
                            for check_name, command, output in failures
                        ]
                    )
                    raise PreflightError(failures)
            return await _work(name, runner, loop)
    finally:
        if lock is not None and lock.locked():
            lock.release()
