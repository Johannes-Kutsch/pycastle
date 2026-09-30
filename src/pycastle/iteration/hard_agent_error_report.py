"""Hard-agent-error abort translation for the pycastle iteration pipeline.

This module owns only the HardAgentError abort pipeline: envelope parse,
title/body composition, filing, printing, and AbortedHardApiError construction.

The private ``_ParsedEnvelope`` dataclass carries optional envelope-derived text
and optional envelope-derived status code. The private ``_parse_envelope``
function walks the raw JSON envelope exactly once and returns a
``_ParsedEnvelope``; all envelope-shape knowledge (key precedence, type guards,
boolean rejection) lives there. Defaults (fall back to raw input for text; fall
back to the exception's own status_code for status) are applied at the call site
in ``translate_hard_agent_error_to_abort``.

It does not own usage-limit-parse-failure filing, AbortedSetup filing,
merge-close-failure filing, operator-actionable git filing, or credential-failure
routing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycastle.iteration import AbortedHardApiError
from pycastle.upstream_issue_report import BUG_AND_TRIAGE_LABELS, hard_agent_error_body

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_runtime.errors import HardAgentError

    from pycastle.config import Config
    from pycastle.display.status_display import StatusDisplay

_SERVICE_LABEL_MAP = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode": "OpenCode",
}


@dataclass(frozen=True)
class _ParsedEnvelope:
    text: str | None
    status_code: int | None


def _parse_envelope(raw: str) -> _ParsedEnvelope:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _ParsedEnvelope(text=None, status_code=None)

    text: str | None = None
    if isinstance(parsed, dict):
        if parsed.get("result"):
            text = str(parsed["result"])
        else:
            error = parsed.get("error")
            if isinstance(error, dict):
                data = error.get("data")
                if isinstance(data, dict) and data.get("message"):
                    text = str(data["message"])
                elif not isinstance(data, dict) and error.get("message"):
                    text = str(error["message"])

    status_code: int | None = None
    if isinstance(parsed, dict):
        status = parsed.get("status")
        status_code = (
            status if isinstance(status, int) and not isinstance(status, bool) else None
        )

    return _ParsedEnvelope(text=text, status_code=status_code)


def translate_hard_agent_error_to_abort(
    err: HardAgentError,
    cfg: Config,
    status_display: StatusDisplay,
    bug_filer: Callable[..., str | None],
) -> AbortedHardApiError:
    """Translate a HardAgentError into AbortedHardApiError.

    Parses the raw envelope once via _parse_envelope, synthesizes a bug-report
    title and body, files the report via the injected bug_filer callable, prints
    a status message via the injected StatusDisplay, and returns
    AbortedHardApiError. Does not handle credential failures.
    """
    raw: str = err.args[0] if err.args else ""
    service_name: str = getattr(err, "service_name", "claude") or "claude"

    envelope = _parse_envelope(raw)
    error_text = envelope.text if envelope.text is not None else raw
    effective_status_code = (
        envelope.status_code
        if envelope.status_code is not None
        else getattr(err, "status_code", None)
    )
    first_line = next(iter(error_text.splitlines()), "") or str(err) or "<unknown>"
    service_label = _SERVICE_LABEL_MAP.get(service_name, service_name)

    title = f"[pycastle] {service_label} API {effective_status_code}: {first_line}"
    body = hard_agent_error_body(
        raw=raw,
        effective_status_code=effective_status_code,
        caller=err.caller,
        service_name=service_name,
    )
    url = bug_filer(title, body, BUG_AND_TRIAGE_LABELS, cfg=cfg)

    status_code_str = (
        str(effective_status_code) if effective_status_code is not None else "no status"
    )
    status_display.print(
        err.caller,
        f"hard API error: status {status_code_str}" + (f" — {url}" if url else ""),
    )
    return AbortedHardApiError(status_code=effective_status_code)
