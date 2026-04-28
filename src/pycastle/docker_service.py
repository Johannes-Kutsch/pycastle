from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import DockerBuildError, DockerServiceError


class DockerService:
    def build_image(
        self,
        image_name: str,
        dockerfile_path: Path | str,
        context_dir: Path | str,
        *,
        no_cache: bool = False,
        python_version: str | None = None,
    ) -> None:
        cmd = ["docker", "build"]
        if no_cache:
            cmd.append("--no-cache")
        cmd += ["-t", image_name, "-f", str(dockerfile_path)]
        if python_version is not None:
            cmd += ["--build-arg", f"PYTHON_VERSION={python_version}"]
        cmd.append(str(context_dir))

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise DockerServiceError(
                "docker not found; ensure it is installed and on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerBuildError(f"docker build timed out: {exc}") from exc

        if result.returncode != 0:
            raise DockerBuildError(
                f"docker build failed (exit {result.returncode}): {result.stderr.strip()}"
            )
