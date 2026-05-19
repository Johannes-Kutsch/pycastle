from __future__ import annotations

import enum
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from ..errors import DockerBuildError, DockerServiceError


class BuildOutcome(enum.Enum):
    REBUILT = "rebuilt"
    FULL_CACHE_HIT = "full_cache_hit"


def _is_full_cache_hit(output: str) -> bool:
    lines = output.splitlines()

    # Classic builder: look for Step N/M lines and check each for ---> Using cache
    classic_steps = [
        i for i, line in enumerate(lines) if re.match(r"^Step \d+/\d+ :", line)
    ]
    if classic_steps:
        for i in classic_steps:
            cached = any(
                "---> Using cache" in lines[j]
                for j in range(i + 1, min(i + 5, len(lines)))
                if not re.match(r"^Step \d+/\d+ :", lines[j])
            )
            if not cached:
                return False
        return True

    # BuildKit: CACHED means cached, DONE means executed (rebuilt)
    has_cached = any(re.match(r"^#\d+\s+CACHED\s*$", line.strip()) for line in lines)
    has_done = any(re.match(r"^#\d+\s+DONE\s+", line.strip()) for line in lines)

    return has_cached and not has_done


class DockerService:
    def build_image(
        self,
        image_name: str,
        dockerfile_path: Path | str,
        context_dir: Path | str,
        *,
        no_cache: bool = False,
        python_version: str | None = None,
        timeout: float | None = None,
        stream: bool = False,
        on_rebuild_start: Callable[[], None] | None = None,
    ) -> BuildOutcome | None:
        if not image_name:
            raise ValueError("image_name must not be empty")
        cmd = ["docker", "build"]
        if no_cache:
            cmd.append("--no-cache")
        cmd += ["-t", image_name, "-f", str(dockerfile_path)]
        if python_version is not None:
            cmd += ["--build-arg", f"PYTHON_VERSION={python_version}"]
        cmd.append(str(context_dir))

        if stream:
            return self._build_streaming(cmd, timeout, on_rebuild_start)

        try:
            result = subprocess.run(cmd, timeout=timeout)
        except FileNotFoundError as exc:
            raise DockerServiceError(
                "docker not found; ensure it is installed and on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerBuildError(f"docker build timed out: {exc}") from exc

        if result.returncode != 0:
            raise DockerBuildError(f"docker build failed (exit {result.returncode})")

        return None

    def _build_streaming(
        self,
        cmd: list[str],
        timeout: float | None,
        on_rebuild_start: Callable[[], None] | None = None,
    ) -> BuildOutcome:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise DockerServiceError(
                "docker not found; ensure it is installed and on PATH"
            ) from exc

        assert proc.stdout is not None
        lines: list[str] = []
        rebuild_signaled = False
        pending_classic_step = False
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)

            if not rebuild_signaled and on_rebuild_start is not None:
                stripped = line.strip()
                if re.match(r"#\d+\s+DONE\s+", stripped):
                    on_rebuild_start()
                    rebuild_signaled = True
                elif re.match(r"Step \d+/\d+ :", stripped):
                    pending_classic_step = True
                elif pending_classic_step:
                    if "---> Using cache" not in stripped and stripped:
                        on_rebuild_start()
                        rebuild_signaled = True
                    pending_classic_step = False

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise DockerBuildError(f"docker build timed out: {exc}") from exc

        if returncode != 0:
            raise DockerBuildError(f"docker build failed (exit {returncode})")

        output = "".join(lines)
        return (
            BuildOutcome.FULL_CACHE_HIT
            if _is_full_cache_hit(output)
            else BuildOutcome.REBUILT
        )
