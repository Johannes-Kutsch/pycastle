import asyncio
import io
import os
import re
import sys
import tarfile
from pathlib import Path

import docker

from .config import DOCKER_IMAGE, LOGS_DIR


class ContainerRunner:
    def __init__(
        self,
        name: str,
        mount_path: Path,
        env: dict[str, str],
        branch: str | None = None,
    ):
        self.name = name
        self.mount_path = mount_path
        self.env = env
        self.branch = branch
        self._client = docker.from_env()
        self._container = None
        self._container_env: dict[str, str] = {}
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self._log_path = LOGS_DIR / f"{slug}.log"

    def __enter__(self) -> "ContainerRunner":
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        host_path = str(self.mount_path.resolve()).replace("\\", "/")

        if self.branch:
            # Mount the main repo at /home/agent/repo; worktree is created inside
            bind_point = "/home/agent/repo"
            working_dir = "/home/agent"  # workspace doesn't exist until worktree add runs
        else:
            bind_point = "/home/agent/workspace"
            working_dir = "/home/agent/workspace"

        # CLAUDE_ACCOUNT_JSON is written as a file inside the container, not passed as env var
        self._container_env = {k: v for k, v in self.env.items() if k != "CLAUDE_ACCOUNT_JSON"}

        self._container = self._client.containers.run(
            DOCKER_IMAGE,
            detach=True,
            volumes={host_path: {"bind": bind_point, "mode": "rw"}},
            environment=self._container_env,
            working_dir=working_dir,
        )

        claude_json = self.env.get("CLAUDE_ACCOUNT_JSON")
        if claude_json:
            self.write_file(claude_json, "/home/agent/.claude.json")

        if self.branch:
            # Try to check out existing branch; fall back to creating it
            self._container.exec_run(
                ["bash", "-c",
                 f"git -C /home/agent/repo worktree add /home/agent/workspace {self.branch} "
                 f"|| git -C /home/agent/repo worktree add -b {self.branch} /home/agent/workspace"],
                environment=self._container_env,
            )

        return self

    def __exit__(self, *_):
        if self._container:
            if self.branch:
                self._container.exec_run(
                    ["bash", "-c",
                     "git -C /home/agent/repo worktree remove --force /home/agent/workspace"],
                    environment=self._container_env,
                )
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

        result_holder: list = [None]
        exc_holder: list = [None]

        def _run():
            try:
                result_holder[0] = self._container.exec_run(
                    ["bash", "-c", command],
                    demux=True,
                    workdir="/home/agent/workspace",
                    environment=self.env,
                )
            except Exception as exc:
                exc_holder[0] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            raise TimeoutError(f"Command timed out after {timeout}s: {command}")

        if exc_holder[0]:
            raise exc_holder[0]

        result = result_holder[0]
        stdout = (result.output[0] or b"").decode("utf-8", errors="replace")
        stderr = (result.output[1] or b"").decode("utf-8", errors="replace")
        if result.exit_code != 0:
            raise RuntimeError(
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
        self._container.put_archive(os.path.dirname(container_path), buf)

    def run_streaming(self) -> str:
        result = self._container.exec_run(
            ["bash", "-c", "claude --print < /tmp/prompt.md"],
            stream=True,
            workdir="/home/agent/workspace",
        )
        parts: list[str] = []
        with open(self._log_path, "w", encoding="utf-8") as log:
            for chunk in result.output:
                text = chunk.decode("utf-8", errors="replace")
                print(text, end="", flush=True)
                log.write(text)
                log.flush()
                parts.append(text)
        return "".join(parts)


async def run_agent(
    name: str,
    prompt_file: Path,
    mount_path: Path,
    env: dict[str, str],
    prompt_args: dict[str, str] | None = None,
    branch: str | None = None,
    exec_timeout: float | None = None,
) -> str:
    from .prompt_pipeline import prepare_prompt

    print(f"\n[{name}] Started")

    loop = asyncio.get_event_loop()
    runner = ContainerRunner(name, mount_path, env, branch=branch)
    # Wrap blocking Docker setup in executor so the event loop stays responsive
    await loop.run_in_executor(None, runner.__enter__)
    try:
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

        prompt = await prepare_prompt(prompt_file, prompt_args or {}, container_exec)
        runner.write_file(prompt, "/tmp/prompt.md")

        return await loop.run_in_executor(None, runner.run_streaming)
    finally:
        runner.__exit__(None, None, None)
