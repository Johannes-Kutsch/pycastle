import subprocess
from pathlib import Path
from typing import Any, ClassVar


class _SubprocessService:
    _timeout_error_class: ClassVar[Any]
    _not_found_error_class: ClassVar[Any]
    _command_error_class: ClassVar[Any]

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def _run(
        self, cmd: list[str], cwd: Path | None = None, **kwargs: object
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        kwargs.setdefault("timeout", self.timeout)
        try:
            return subprocess.run(cmd, cwd=cwd, **kwargs)  # type: ignore[call-overload]
        except subprocess.TimeoutExpired as exc:
            raise self._timeout_error_class(
                f"command timed out after {self.timeout}s: {exc.cmd}"
            ) from exc
        except FileNotFoundError as exc:
            raise self._not_found_error_class(
                f"executable not found: {cmd[0]}"
            ) from exc

    def _run_or_raise(
        self, cmd: list[str], message: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        result = self._run(cmd, cwd=cwd, capture_output=True)
        if result.returncode != 0:
            raise self._command_error_class(
                message, result.returncode, self._decode(result.stderr)
            )
        return result

    @staticmethod
    def _decode(b: bytes) -> str:
        return b.decode("utf-8", errors="replace").strip()
