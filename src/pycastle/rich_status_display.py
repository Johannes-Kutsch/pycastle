import re
import threading
import time

from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.padding import Padding
from rich.table import Table
from rich.text import Text


_PALETTE: list[tuple[int, int, int]] = [
    (149, 97, 226),  # 0 deep purple
    (255, 140, 50),  # 1 deep orange
    (240, 205, 45),  # 2 deep yellow
    (185, 154, 235),  # 3 mid purple
    (255, 185, 120),  # 4 mid orange
    (248, 228, 130),  # 5 mid yellow
    (215, 198, 248),  # 6 pale purple
    (255, 215, 185),  # 7 pale orange
    (253, 244, 195),  # 8 pale yellow
]


def _agent_name_style(name: str) -> str:
    """Return the base Rich style for a caller name's prefix/name-column rendering.

    Agents whose name contains `#N` get a stable color from `_PALETTE`; everything
    else falls back to plain bold.
    """
    m = re.search(r"#(\d+)", name)
    if not m:
        return "bold"
    r, g, b = _PALETTE[int(m.group(1)) % len(_PALETTE)]
    return f"bold rgb({r},{g},{b})"


def _row_priority(name: str, kinds: dict[str, str]) -> int:
    if kinds.get(name) == "phase":
        return -1
    m = re.search(r"#(\d+)", name)
    return int(m.group(1)) if m else 0


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def _format_k(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k"


def _token_style(tokens: int) -> str:
    if tokens > 100_000:
        return "bold rgb(217,119,87)"
    if tokens > 80_000:
        return "bold rgb(212,168,67)"
    return ""


def _token_text(current: int, peak: int) -> Text:
    if current == 0:
        return Text("")
    text = Text()
    text.append(_format_k(current), style=_token_style(current))
    text.append(" (↑")
    text.append(_format_k(peak), style=_token_style(peak))
    text.append(")")
    return text


class _AgentRow:
    __slots__ = (
        "name",
        "phase",
        "work_body",
        "started_at",
        "last_update",
        "current_tokens",
        "peak_tokens",
    )

    def __init__(self, name: str, phase: str, work_body: str = "") -> None:
        self.name = name
        self.phase = phase
        self.work_body = work_body
        now = time.monotonic()
        self.started_at = now
        self.last_update = now
        self.current_tokens = 0
        self.peak_tokens = 0

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
        self._last_kind: str | None = None
        self._kinds: dict[str, str] = {}

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        # Called by Live on each refresh tick from the Live thread.
        # Acquire lock only to snapshot row state, then release before yielding.
        with self._lock:
            rows = sorted(
                self._rows.values(), key=lambda r: _row_priority(r.name, self._kinds)
            )

        table = Table(show_header=False, expand=False, box=None)
        table.add_column(justify="right")  # elapsed
        table.add_column()  # tokens
        table.add_column()  # name
        table.add_column()  # idle
        table.add_column()  # body

        for row in rows:
            name_text = Text()
            base_style = _agent_name_style(row.name)
            for segment in re.split(r"(#\d+)", row.name):
                if segment:
                    if re.fullmatch(r"#\d+", segment):
                        name_text.append(segment, style=f"{base_style} bold cyan")
                    else:
                        name_text.append(segment, style=base_style)

            body = row.work_body if row.phase == "Work" else row.phase

            table.add_row(
                Text(_format_duration(row.elapsed_seconds()), style="dim"),
                _token_text(row.current_tokens, row.peak_tokens),
                name_text,
                Text(_format_duration(row.idle_seconds()), style="dim"),
                Text(body),
            )

        yield Padding(table, (1, 0, 0, 0))

    def _acquire_live(self) -> "Live | None":
        """Create and record a new Live if none is running. Must be called with self._lock held."""
        if self._live is None:
            live = Live(
                self, console=self._console, refresh_per_second=4, transient=True
            )
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
        if caller == "":
            return True
        if caller == self._last_caller:
            return False
        kinds = {self._last_kind, self._kinds.get(caller)}
        if "agent" in kinds and kinds <= {"phase", "agent"}:
            return False
        return True

    def register(
        self,
        caller: str,
        kind: Literal["phase", "agent"],
        startup_message: str = "started",
        work_body: str = "",
        initial_phase: str = "Setup",
    ) -> None:
        with self._lock:
            self._rows[caller] = _AgentRow(caller, initial_phase, work_body)
            if caller != "":
                self._kinds[caller] = kind
            live_to_start = self._acquire_live()
        if live_to_start is not None:
            live_to_start.start()
        self.print(caller, startup_message)

    def update_phase(self, name: str, phase: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].phase = phase
                self._rows[name].last_update = time.monotonic()

    def reset_idle_timer(self, name: str) -> None:
        with self._lock:
            if name in self._rows:
                self._rows[name].last_update = time.monotonic()

    def update_tokens(self, name: str, current_tokens: int) -> None:
        with self._lock:
            if name in self._rows:
                row = self._rows[name]
                row.current_tokens = current_tokens
                row.peak_tokens = max(row.peak_tokens, current_tokens)

    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
        shutdown_style: str = "success",
    ) -> None:
        with self._lock:
            self._rows.pop(caller, None)
            live_to_stop = self._release_live_if_empty()
        if live_to_stop is not None:
            live_to_stop.stop()
        self.print(caller, shutdown_message, style=shutdown_style)
        with self._lock:
            self._kinds.pop(caller, None)

    def print(
        self,
        caller: str,
        message: object,
        style: str | None = None,
    ) -> None:
        style_map = {
            "error": "red",
            "success": "green",
            "warning": "yellow",
            "interrupted": "cyan",
        }
        rich_style = style_map.get(style or "")
        lines = str(message).split("\n")
        with self._lock:
            prepend_blank = self._blank_before(caller)
            self._last_caller = caller
            self._last_kind = self._kinds.get(caller)
        if prepend_blank:
            self._console.print()
        has_issue_number = bool(re.search(r"#\d+", caller)) if caller else False
        for line in lines:
            text = Text()
            if caller:
                base_style = _agent_name_style(caller)
                if has_issue_number:
                    # Split prefix on #N so that segment gets bold cyan overlay.
                    prefix = f"[{caller}]"
                    for seg in re.split(r"(#\d+)", prefix):
                        if not seg:
                            continue
                        if re.fullmatch(r"#\d+", seg):
                            text.append(seg, style=f"{base_style} bold cyan")
                        else:
                            text.append(seg, style=base_style)
                else:
                    text.append(f"[{caller}]", style=base_style)
                body_start = len(text)
                text.append(f" {line}")
            else:
                body_start = 0
                text.append(line)
            if rich_style:
                if has_issue_number and caller:
                    # Style only the body span, preserving the palette-colored prefix.
                    text.stylize(rich_style, start=body_start)
                else:
                    text.stylize(rich_style)
            self._console.print(text)

    def stop(self) -> None:
        live_to_stop: Live | None = None
        with self._lock:
            if self._live is not None:
                live_to_stop = self._live
                self._live = None
        if live_to_stop is not None:
            live_to_stop.stop()
