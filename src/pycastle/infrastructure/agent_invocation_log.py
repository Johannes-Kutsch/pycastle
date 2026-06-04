import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .. import _time as _time_module


class AgentInvocationLog:
    def __init__(
        self,
        *,
        now_local: Callable[[], datetime] | None = None,
    ) -> None:
        self._now_local = now_local or _time_module.now_local

    def reserve(self, *, agent_name: str, effective_logs_dir: Path) -> Path:
        effective_logs_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")
        timestamp = self._now_local().strftime("%Y%m%dT%H%M")
        stem = f"{slug}-{timestamp}"
        for suffix in ["", *[f"-{n}" for n in range(2, 10_000)]]:
            path = effective_logs_dir / f"{stem}{suffix}.log"
            try:
                with open(path, "xb"):
                    pass
                return path
            except FileExistsError:
                continue
        raise RuntimeError(f"could not reserve unique agent log path for {stem}")
