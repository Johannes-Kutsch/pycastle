"""Hard-agent-error abort translation for the pycastle iteration pipeline.

This module owns only the HardAgentError abort pipeline: filing, printing,
and AbortedHardApiError construction.  Envelope parsing, service-label
mapping, title synthesis, and body composition are delegated to
``compose_hard_agent_error_report`` in ``upstream_issue_report``.

It does not own usage-limit-parse-failure filing, AbortedSetup filing,
merge-close-failure filing, operator-actionable git filing, or credential-failure
routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pycastle.iteration import AbortedHardApiError
from pycastle.upstream_issue_report import compose_hard_agent_error_report

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_runtime.errors import HardAgentError

    from pycastle.config import Config
    from pycastle.display.status_display import StatusDisplay


def translate_hard_agent_error_to_abort(
    err: HardAgentError,
    cfg: Config,
    status_display: StatusDisplay,
    bug_filer: Callable[..., str | None],
) -> AbortedHardApiError:
    """Translate a HardAgentError into AbortedHardApiError.

    Delegates title/body/label composition to the shared composer, files the
    report via the injected bug_filer callable, prints a status message via the
    injected StatusDisplay, and returns AbortedHardApiError.  Does not handle
    credential failures.
    """
    composition = compose_hard_agent_error_report(err)
    url = bug_filer(composition.title, composition.body, composition.labels, cfg=cfg)

    status_code_str = (
        str(composition.effective_status_code)
        if composition.effective_status_code is not None
        else "no status"
    )
    status_display.print(
        err.caller,
        f"hard API error: status {status_code_str}" + (f" — {url}" if url else ""),
    )
    return AbortedHardApiError(status_code=composition.effective_status_code)
