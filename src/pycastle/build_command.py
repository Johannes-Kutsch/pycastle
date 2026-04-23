import subprocess
import sys
from pathlib import Path

from .config import DOCKERFILE, DOCKER_IMAGE


def main(no_cache: bool = False) -> None:
    cmd = ["docker", "build", "-t", DOCKER_IMAGE, "-f", str(DOCKERFILE), "."]
    if no_cache:
        cmd.insert(2, "--no-cache")
    python_version_file = Path(".python-version")
    if python_version_file.exists():
        version = python_version_file.read_text().strip()
        parts = version.split(".")
        version = ".".join(parts[:2]) if len(parts) >= 2 else version
        cmd += ["--build-arg", f"PYTHON_VERSION={version}"]
    result = subprocess.run(cmd)
    sys.exit(result.returncode)
