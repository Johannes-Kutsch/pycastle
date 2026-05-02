import re
import threading
import time

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
    if name == "Pre-Flight":
        return "plan"
    if name == "Pre-Flight Reporter":
        return "plan"
    if name == "merge":
        return "merge"
    return ""


def _role_color(name: str) -> str:
    if name == "Planner":
        return "blue"
    if name.startswith("Implementer"):
        return "orange1"
    if name.startswith("Reviewer"):
        return "yellow"
    if name == "Merger":
        return "green"
    if name == "Pre-Flight":
        return "purple"
    if name == "Pre-Flight Reporter":
        return "red"
    if name == "merge":
        return "green"
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
        "work_body",
        "started_at",
        "last_update",
    )

    def __init__(self, name: str, phase: str, work_body: str = "") -> None:
        self.name = name
        self.phase = phase
        self.work_body = work_body
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
        self._last_source: str | None = None

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        # Called by Live on each refresh tick from the Live thread.
        # Acquire lock only to snapshot row state, then release before yielding.
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda r: _sort_key(r.name))

        table = Table(show_header=False, expand=False, box=None)
        table.add_column(justify="right")  # elapsed
        table.add_column()  # name
        table.add_column()  # idle
        table.add_column()  # body

        for row in rows:
            role_color = _role_color(row.name)
            name_text = Text()
            for segment in re.split(r"(\d+)", row.name):
                if segment:
                    if segment.isdigit():
                        name_text.append(segment, style="bold cyan")
                    elif role_color:
                        name_text.append(segment, style=f"bold {role_color}")
                    else:
                        name_text.append(segment, style="bold")

            body = row.work_body if row.phase == "Work" else row.phase

            table.add_row(
                Text(_format_duration(row.elapsed_seconds()), style="dim"),
                name_text,
                Text(_format_duration(row.idle_seconds()), style="dim"),
                Text(body),
            )

        yield Padding(table, (1, 0, 0, 0))

    def add_agent(self, name: str, phase: str, work_body: str = "") -> None:
        live_to_start: Live | None = None
        with self._lock:
            self._rows[name] = _AgentRow(name, phase, work_body)
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

    def reset_idle_timer(self, name: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].last_update = time.monotonic()

    def print(self, message: object, *, source: str = "") -> None:
        with self._lock:
            prepend_blank = self._last_source is not None and source != self._last_source
            self._last_source = source
        if prepend_blank:
            self._console.print()
        self._console.print(message)

    def stop(self) -> None:
        live_to_stop: Live | None = None
        with self._lock:
            if self._live is not None:
                live_to_stop = self._live
                self._live = None
        if live_to_stop is not None:
            live_to_stop.stop()
