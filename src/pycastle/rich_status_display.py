import re
import threading
import time

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

_PHASE_RANK: dict[str, int] = {
    "preflight": -1,
    "plan": 0,
    "implement": 1,
    "review": 2,
    "merge": 3,
}


def _stage_from_name(name: str) -> str:
    # Phase rows
    if name == "Preflight":
        return "preflight"
    if name == "Plan":
        return "plan"
    if name == "Implement":
        return "implement"
    if name == "Merge":
        return "merge"
    # New canonical agent names
    if name == "Preflight Agent":
        return "preflight"
    if name == "Plan Agent":
        return "plan"
    if name.startswith("Implement Agent"):
        return "implement"
    if name.startswith("Review Agent"):
        return "review"
    if name == "Merge Agent":
        return "merge"
    if name == "Pre-Flight Reporter":
        return "plan"
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

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._rows: dict[str, _AgentRow] = {}
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._last_caller: str | None = None

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
            name_text = Text()
            for segment in re.split(r"(\d+)", row.name):
                if segment:
                    if segment.isdigit():
                        name_text.append(segment, style="bold cyan")
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

    def _acquire_live(self) -> "Live | None":
        """Create and record a new Live if none is running. Must be called with self._lock held."""
        if self._live is None:
            live = Live(self, console=self._console, refresh_per_second=4, transient=True)
            self._live = live
            return live
        return None

    def _release_live_if_empty(self) -> "Live | None":
        """Return the Live for stopping if no rows remain. Must be called with self._lock held."""
        if not self._rows and self._live is not None:
            live, self._live = self._live, None
            return live
        return None

    def _blank_before(self, caller: str) -> bool:
        return self._last_caller is not None and (caller != self._last_caller or caller == "")

    def register(
        self, caller: str, startup_message: str = "started", work_body: str = ""
    ) -> None:
        with self._lock:
            prepend_blank = self._blank_before(caller)
            self._last_caller = caller
            self._rows[caller] = _AgentRow(caller, "Setup", work_body)
            live_to_start = self._acquire_live()
        if live_to_start is not None:
            live_to_start.start()
        if prepend_blank:
            self._console.print()
        line = f"[{caller}] {startup_message}" if caller else startup_message
        self._console.print(Text(line))

    def update_phase(self, name: str, phase: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].phase = phase
                self._rows[name].last_update = time.monotonic()

    def reset_idle_timer(self, name: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].last_update = time.monotonic()

    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
        shutdown_style: str = "success",
    ) -> None:
        with self._lock:
            prepend_blank = self._blank_before(caller)
            self._last_caller = caller
            self._rows.pop(caller, None)
            live_to_stop = self._release_live_if_empty()
        if live_to_stop is not None:
            live_to_stop.stop()
        if prepend_blank:
            self._console.print()
        line = f"[{caller}] {shutdown_message}" if caller else shutdown_message
        text = Text(line)
        if shutdown_style == "success":
            text.stylize("green")
        elif shutdown_style == "error":
            text.stylize("red")
        self._console.print(text)

    def print(
        self,
        caller: str,
        message: object,
        style: str | None = None,
    ) -> None:
        with self._lock:
            prepend_blank = self._blank_before(caller)
            self._last_caller = caller
        if prepend_blank:
            self._console.print()
        if caller:
            text = Text()
            text.append(f"[{caller}]", style="bold")
            text.append(f" {message}")
        else:
            text = Text(str(message))
        if style == "error":
            text.stylize("red")
        elif style == "success":
            text.stylize("green")
        self._console.print(text)

    def stop(self) -> None:
        live_to_stop: Live | None = None
        with self._lock:
            if self._live is not None:
                live_to_stop = self._live
                self._live = None
        if live_to_stop is not None:
            live_to_stop.stop()
