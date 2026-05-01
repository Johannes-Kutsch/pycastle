import re
import threading
import time
from pathlib import Path

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.padding import Padding
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


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


class _AgentRow:
    __slots__ = (
        "name",
        "phase",
        "log_path",
        "issue_title",
        "started_at",
        "last_update",
        "last_message",
    )

    def __init__(self, name: str, phase: str, log_path: Path, issue_title: str) -> None:
        self.name = name
        self.phase = phase
        self.log_path = log_path
        self.issue_title = issue_title
        self.last_message = ""
        now = time.monotonic()
        self.started_at = now
        self.last_update = now

    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

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

        table = Table(show_header=False, expand=False, box=None)
        table.add_column(justify="right")  # elapsed
        table.add_column()  # name + headline
        table.add_column()  # phase
        table.add_column()  # idle
        table.add_column(overflow="ellipsis", no_wrap=True)  # last message

        for row in rows:
            abs_uri = row.log_path.resolve().as_uri()
            name_text = Text()
            name_text.append(row.name, style=f"link {abs_uri}")
            if row.issue_title:
                name_text.append(f" - {row.issue_title}")
            table.add_row(
                _format_duration(row.elapsed_seconds()),
                name_text,
                row.phase,
                _format_duration(row.idle_seconds()),
                row.last_message,
            )

        yield Padding(table, (1, 0, 0, 0))

    def add_agent(
        self, name: str, phase: str, log_path: Path, issue_title: str
    ) -> None:
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

    def update_message(self, name: str, message: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].last_message = message
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

    def reset_idle_timer(self, name: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].last_update = time.monotonic()

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
