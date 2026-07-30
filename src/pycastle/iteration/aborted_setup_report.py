"""AbortedSetup upstream-report translation for the pycastle iteration pipeline.

This module owns only the AbortedSetup abort pipeline: title/body composition,
bug filing, status printing, and returning ExitFailure(code=1).

It does not own HardAgentError filing, usage-limit-parse-failure filing,
merge-close-failure filing, operator-actionable git filing, or credential-failure
routing.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pycastle.bug_reporter import BUG_REPORT_LABEL_LIST

if TYPE_CHECKING:
    from collections.abc import Callable

    from pycastle.config import Config
    from pycastle.display.status_display import StatusDisplay
    from pycastle.iteration import AbortedSetup


@dataclasses.dataclass(frozen=True)
class ExitFailure:
    code: int


def translate_aborted_setup_to_directive(
    outcome: AbortedSetup,
    cfg: Config,
    status_display: StatusDisplay,
    bug_filer: Callable[..., str | None],
) -> ExitFailure:
    """Translate an AbortedSetup outcome into an ExitFailure(code=1) directive.

    Synthesizes a bug-report title and body from the outcome, files the report
    via the injected bug_filer callable, prints a status message via the injected
    StatusDisplay, and returns ExitFailure(code=1).
    """
    phase = outcome.phase
    message = outcome.message
    command = outcome.command
    output = outcome.output

    first_line = next(iter(message.splitlines()), "")
    title = f"[pycastle] {phase} setup failure: {first_line}"
    body_parts = [
        "## Setup phase failure\n",
        f"Phase: {phase}\n",
        f"```\n{message}\n```\n",
    ]
    if command:
        body_parts.append(f"Command: `{command}`\n")
    if output:
        body_parts.append(f"Output:\n\n```\n{output}\n```\n")
    body = "\n".join(body_parts)
    url = bug_filer(title, body, BUG_REPORT_LABEL_LIST, cfg=cfg)

    local_parts = [f"{phase} setup failed: {message}"]
    if command:
        local_parts.append(f"Command: {command}")
    if output:
        local_parts.append(f"Output: {output}")
    status_display.print(
        "",
        "\n".join(local_parts) + (f"\nReport: {url}" if url else ""),
    )
    return ExitFailure(code=1)
