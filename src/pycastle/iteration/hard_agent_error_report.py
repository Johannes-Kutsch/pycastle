"""Hard-agent-error abort translation for the pycastle iteration pipeline.

This module owns only the HardAgentError abort pipeline: envelope extraction,
status-code extraction, service-label mapping, title/body composition, bug filing,
status printing, and returning AbortedHardApiError.

It does not own usage-limit-parse-failure filing, AbortedSetup filing,
merge-close-failure filing, operator-actionable git filing, or credential-failure
routing.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from agent_runtime.errors import HardAgentError

from ..bug_reporter import BUG_REPORT_LABEL_LIST
from ..config import Config
from ..display.status_display import StatusDisplay
from . import AbortedHardApiError

_SERVICE_LABEL_MAP = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode": "OpenCode",
}


def _extract_envelope_text(raw: str) -> str:
    error_text = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("result"):
            error_text = str(parsed["result"])
        elif isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                data = error.get("data")
                if isinstance(data, dict) and data.get("message"):
                    error_text = str(data["message"])
                elif not isinstance(data, dict) and error.get("message"):
                    error_text = str(error["message"])
    except (json.JSONDecodeError, TypeError):
        pass
    return error_text


def _extract_envelope_status_code(raw: str, fallback: int | None) -> int | None:
    if fallback is not None:
        return fallback
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    status = parsed.get("status")
    return (
        status if isinstance(status, int) and not isinstance(status, bool) else fallback
    )


def translate_hard_agent_error_to_abort(
    err: HardAgentError,
    cfg: Config,
    status_display: StatusDisplay,
    bug_filer: Callable[..., str | None],
) -> AbortedHardApiError:
    """Translate a HardAgentError into AbortedHardApiError.

    Extracts envelope text and status code, synthesizes a bug-report title and body,
    files the report via the injected bug_filer callable, prints a status message via
    the injected StatusDisplay, and returns AbortedHardApiError. Does not handle
    credential failures.
    """
    raw: str = err.args[0] if err.args else ""
    service_name: str = getattr(err, "service_name", "claude") or "claude"

    effective_status_code = _extract_envelope_status_code(
        raw, getattr(err, "status_code", None)
    )
    error_text = _extract_envelope_text(raw)
    first_line = next(iter(error_text.splitlines()), "") or str(err) or "<unknown>"
    service_label = _SERVICE_LABEL_MAP.get(service_name, service_name)

    title = f"[pycastle] {service_label} API {effective_status_code}: {first_line}"
    body = (
        f"## Raw result envelope\n\n```json\n{raw}\n```\n\n"
        f"Status: {effective_status_code}\n"
        f"Agent: {err.caller or '<unknown>'}\n"
        f"Service: {service_name}\n"
    )
    url = bug_filer(title, body, BUG_REPORT_LABEL_LIST, cfg=cfg)

    status_code_str = (
        str(effective_status_code) if effective_status_code is not None else "no status"
    )
    status_display.print(
        err.caller,
        f"hard API error: status {status_code_str}" + (f" — {url}" if url else ""),
    )
    return AbortedHardApiError(status_code=effective_status_code)
