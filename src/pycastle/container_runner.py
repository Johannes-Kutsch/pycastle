import asyncio
import io
import os
import queue
import re
import shlex
import sys
import tarfile
import threading
from collections.abc import Callable, Generator
from pathlib import Path

import docker
from docker.models.containers import Container as DockerContainer

from .agent_output_protocol import AgentOutput, AgentRole, process_stream
from .config import Config
from .errors import (
    AgentTimeoutError,
    DockerError,
    DockerTimeoutError,
    UsageLimitError,
)
from .status_display import PlainStatusDisplay
from .worktree import (
    CONTAINER_PARENT_GIT,
    patch_gitdir_for_container,
)


def _build_claude_command(model: str = "", effort: str = "") -> str:
    flags = "--verbose --dangerously-skip-permissions --output-format stream-json -p -"
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
        docker_client=None,
        status_display=None,
        *,
        cfg: Config,
    ):
        self.name = name
        self.mount_path = mount_path
        self.env = env
        self.branch = branch
        self.worktree_host_path = worktree_host_path
        self.gitdir_overlay = gitdir_overlay
        self.model = model
        self.effort = effort
        self._cfg = cfg
        if status_display is None:
            status_display = PlainStatusDisplay()
        self._status_display = status_display
        self._owns_client = docker_client is None
        self._client = docker_client if docker_client is not None else docker.from_env()
        self._container: DockerContainer | None = None
        self._container_env: dict[str, str] = {}
        self._prompt: str = ""
        self._auto_overlay: Path | None = None
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self._log_path = self._cfg.logs_dir / f"{slug}.log"
        self._worktree_path = "/home/agent/workspace"

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def _active_container(self) -> DockerContainer:
        if self._container is None:
            raise DockerError("Container not started")
        return self._container

    def __enter__(self) -> "ContainerRunner":
        self._cfg.logs_dir.mkdir(parents=True, exist_ok=True)
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
            git_file = self.mount_path / ".git"
            if git_file.is_file():
                overlay = patch_gitdir_for_container(self.mount_path)
                parent_git = self._parse_parent_git(git_file)
                if parent_git is not None and parent_git.exists():
                    parent_git_str = str(parent_git.resolve()).replace("\\", "/")
                    volumes = {
                        repo_path: {"bind": "/home/agent/workspace", "mode": "rw"},
                        parent_git_str: {"bind": CONTAINER_PARENT_GIT, "mode": "rw"},
                    }
                    if overlay is not None:
                        self._auto_overlay = overlay
                        overlay_path = str(overlay.resolve()).replace("\\", "/")
                        volumes[overlay_path] = {
                            "bind": "/home/agent/workspace/.git",
                            "mode": "ro",
                        }
                else:
                    volumes = {
                        repo_path: {"bind": "/home/agent/workspace", "mode": "rw"}
                    }
            else:
                volumes = {repo_path: {"bind": "/home/agent/workspace", "mode": "rw"}}
        working_dir = "/home/agent/workspace"

        # CLAUDE_ACCOUNT_JSON is written as a file inside the container, not passed as env var
        self._container_env = {
            k: v for k, v in self.env.items() if k != "CLAUDE_ACCOUNT_JSON"
        }

        self._container = self._client.containers.run(
            self._cfg.docker_image_name,
            detach=True,
            volumes=volumes,
            environment=self._container_env,
            working_dir=working_dir,
        )

        claude_json = self.env.get("CLAUDE_ACCOUNT_JSON")
        if claude_json:
            self.write_file(claude_json, "/home/agent/.claude.json")

        return self

    @staticmethod
    def _parse_parent_git(git_file: Path) -> Path | None:
        m = re.search(r"gitdir:\s*(.+)", git_file.read_text(encoding="utf-8"))
        if not m:
            return None
        gitdir = m.group(1).strip().replace("\\", "/")
        idx = gitdir.find(".git/worktrees/")
        if idx == -1:
            return None
        return Path(gitdir[:idx] + ".git")

    def __exit__(self, *_):
        if self._auto_overlay:
            try:
                self._auto_overlay.unlink(missing_ok=True)
            except Exception:
                pass
            self._auto_overlay = None
        if self._container:
            try:
                self._container.stop(timeout=5)
            except Exception:
                pass
            try:
                self._container.remove(force=True)
            except Exception:
                pass
        if self._owns_client:
            try:
                self._client.close()
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

    async def setup(self, git_name: str, git_email: str, work_body: str = "") -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.__enter__)
        self._status_display.register(self.name, work_body=work_body)
        await loop.run_in_executor(
            None,
            self.exec_simple,
            f"git config --global user.name {shlex.quote(git_name)}",
        )
        await loop.run_in_executor(
            None,
            self.exec_simple,
            f"git config --global user.email {shlex.quote(git_email)}",
        )
        await loop.run_in_executor(
            None,
            self.exec_simple,
            "pip install -e '.[dev]' || pip install -r requirements.txt",
        )

    async def preflight(
        self, checks: list[tuple[str, str]]
    ) -> list[tuple[str, str, str]]:
        loop = asyncio.get_running_loop()
        failures: list[tuple[str, str, str]] = []
        for check_name, command in checks:
            self._status_display.update_phase(self.name, f"Running {check_name} Checks")
            try:
                await loop.run_in_executor(None, self.exec_simple, command)
            except DockerError as exc:
                failures.append((check_name, command, str(exc)))
        return failures

    async def prepare(self, prompt_file: Path, prompt_args: dict[str, str]) -> None:
        from .prompt_pipeline import prepare_prompt

        self._status_display.update_phase(self.name, "Prepare")
        loop = asyncio.get_running_loop()

        async def container_exec(cmd: str) -> str:
            return await loop.run_in_executor(None, self.exec_simple, cmd)

        self._prompt = await prepare_prompt(prompt_file, prompt_args, container_exec)

    async def work(self, role: AgentRole) -> AgentOutput:
        self._status_display.update_phase(self.name, "Work")
        loop = asyncio.get_running_loop()
        on_turn: Callable[[str], None] = lambda turn: self._status_display.print(
            self.name, turn
        )
        return await loop.run_in_executor(
            None, lambda: self.run_streaming(role=role, on_turn=on_turn)
        )

    def run_streaming(self, role: AgentRole, on_turn: Callable[[str], None]) -> AgentOutput:
        self.write_file(self._prompt, "/tmp/.pycastle_prompt")
        exec_result = self._active_container.exec_run(
            ["bash", "-c", _build_claude_command(model=self.model, effort=self.effort)],
            stream=True,
            workdir=self._worktree_path,
        )

        q: queue.Queue = queue.Queue()
        _sentinel = object()

        def _feed():
            try:
                for chunk in exec_result.output:
                    q.put(chunk)
            finally:
                q.put(_sentinel)

        threading.Thread(target=_feed, daemon=True).start()

        log = open(self._log_path, "wb")  # noqa: WPS515
        try:
            def _lines() -> Generator[str, None, None]:
                line_buf = ""
                while True:
                    try:
                        chunk = q.get(timeout=self._cfg.idle_timeout)
                    except queue.Empty:
                        raise AgentTimeoutError(
                            f"Agent idle for more than {self._cfg.idle_timeout}s"
                        )
                    if chunk is _sentinel:
                        return
                    log.write(chunk)
                    log.flush()
                    text = chunk.decode("utf-8", errors="replace")
                    self._status_display.reset_idle_timer(self.name)
                    line_buf += text
                    while "\n" in line_buf:
                        line, line_buf = line_buf.split("\n", 1)
                        yield line

            return process_stream(_lines(), on_turn, role, self._cfg.usage_limit_patterns)
        finally:
            log.close()
            try:
                self._active_container.exec_run(
                    ["bash", "-c", "rm -f /tmp/.pycastle_prompt"],
                    workdir=self._worktree_path,
                )
            except Exception:
                pass
