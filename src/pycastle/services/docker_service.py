from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from ..errors import DockerBuildError, DockerServiceError
from ._docker_build_output import (
    BuildOutcome,
    DockerBuildOutputInterpreter,
)


_PROGRESS_PREFIX = "Building Docker Image · "


class _ProgressWriter:
    """Writes terse build-progress lines to stdout; handles TTY vs non-TTY."""

    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty
        self._current: str | None = None

    def update(self, suffix: str) -> None:
        if suffix == self._current:
            return
        self._current = suffix
        text = _PROGRESS_PREFIX + suffix
        if self._is_tty:
            sys.stdout.write(f"\r\x1b[K{text}")
        else:
            sys.stdout.write(f"{text}\n")
        sys.stdout.flush()

    def finish(self, suffix: str) -> None:
        text = _PROGRESS_PREFIX + suffix
        if self._is_tty:
            sys.stdout.write(f"\r\x1b[K{text}\n")
        else:
            sys.stdout.write(f"{text}\n")
        sys.stdout.flush()
        self._current = None

    def clear(self) -> None:
        if self._is_tty and self._current is not None:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
            self._current = None


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
        terse: bool = False,
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

        if stream and terse:
            return self._build_terse(cmd, timeout)

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

    def _build_terse(
        self,
        cmd: list[str],
        timeout: float | None,
    ) -> BuildOutcome:
        writer = _ProgressWriter(sys.stdout.isatty())
        writer.update("preparing…")
        interpreter = DockerBuildOutputInterpreter()

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

        for line in proc.stdout:
            lines.append(line)
            interpretation = interpreter.observe_line(line)
            if interpretation.progress_text is not None:
                writer.update(interpretation.progress_text)

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise DockerBuildError(f"docker build timed out: {exc}") from exc

        if returncode != 0:
            writer.clear()
            writer.finish("failed")
            sys.stdout.write("".join(lines))
            sys.stdout.flush()
            raise DockerBuildError(f"docker build failed (exit {returncode})")

        outcome = interpreter.final_outcome
        if outcome == BuildOutcome.FULL_CACHE_HIT:
            writer.finish("up to date")
            return outcome

        writer.finish("completed")
        return outcome

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
        interpreter = DockerBuildOutputInterpreter(on_rebuild_start=on_rebuild_start)
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            interpreter.observe_line(line)

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise DockerBuildError(f"docker build timed out: {exc}") from exc

        if returncode != 0:
            raise DockerBuildError(f"docker build failed (exit {returncode})")

        return interpreter.final_outcome
