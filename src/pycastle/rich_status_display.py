import re
import threading
import time
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.table import Table
from rich.text import Text

_PHASE_RANK: dict[str, int] = {
    "plan": 0,
    "implement": 1,
    "review": 2,
    "merge": 3,
}


def _stage_from_name(name: str) -> str:
    if name == "Planner":
        return "plan"
    if name.startswith("Implementer"):
        return "implement"
    if name.startswith("Reviewer"):
        return "review"
    if name == "Merger":
        return "merge"
    return ""


def _sort_key(name: str) -> tuple[int, int]:
    rank = _PHASE_RANK.get(_stage_from_name(name), 99)
    m = re.search(r"\d+", name)
    return (rank, int(m.group()) if m else 0)


class _AgentRow:
    __slots__ = ("name", "phase", "log_path", "issue_title", "last_update")

    def __init__(self, name: str, phase: str, log_path: Path, issue_title: str) -> None:
        self.name = name
        self.phase = phase
        self.log_path = log_path
        self.issue_title = issue_title
        self.last_update = time.monotonic()

    def idle_seconds(self) -> int:
        return int(time.monotonic() - self.last_update)


class RichStatusDisplay:
    """Live terminal status panel showing one row per active agent."""

    def __init__(self) -> None:
        self._console = Console()
        self._rows: dict[str, _AgentRow] = {}
        self._lock = threading.Lock()
        self._live: Live | None = None

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        # Called by Live on each refresh tick from the Live thread.
        # Acquire lock only to snapshot row state, then release before yielding.
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda r: _sort_key(r.name))

        table = Table(show_header=True, header_style="bold", expand=False, box=None)
        table.add_column("Agent")
        table.add_column("Phase")
        table.add_column("Idle (s)", justify="right")
        table.add_column("Log")

        for row in rows:
            abs_uri = row.log_path.resolve().as_uri()
            link = Text(str(row.log_path), style=f"link {abs_uri}")
            table.add_row(row.name, row.phase, str(row.idle_seconds()), link)

        yield table

    def add_agent(self, name: str, phase: str, log_path: Path, issue_title: str) -> None:
        live_to_start: Live | None = None
        with self._lock:
            self._rows[name] = _AgentRow(name, phase, log_path, issue_title)
            if self._live is None:
                live = Live(
                    self,
                    console=self._console,
                    refresh_per_second=4,
                    transient=True,
                )
                self._live = live
                live_to_start = live
        if live_to_start is not None:
            live_to_start.start()

    def update_phase(self, name: str, phase: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].phase = phase
                self._rows[name].last_update = time.monotonic()

    def remove_agent(self, name: str) -> None:
        live_to_stop: Live | None = None
        with self._lock:
            self._rows.pop(name, None)
            if not self._rows and self._live is not None:
                live_to_stop = self._live
                self._live = None
        if live_to_stop is not None:
            live_to_stop.stop()

    def print(self, message: str) -> None:
        self._console.print(message)

    def stop(self) -> None:
        live_to_stop: Live | None = None
        with self._lock:
            if self._live is not None:
                live_to_stop = self._live
                self._live = None
        if live_to_stop is not None:
            live_to_stop.stop()
