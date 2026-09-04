"""Upstream issue report — structured report filing beside existing filers.

This module exposes a report-description dataclass, a shared filer function, an
env-block helper used internally, per-report body composer functions, and the
shared ``bug + needs-triage`` label list constant.

Body composers are pure functions: they return a body string and have no
knowledge of labels, dedupe, echo, or ``GithubService``. The filer prepends the
env block; callers do not.

The "reporter must never raise" invariant is preserved: ``GithubServiceError``
is swallowed on both search and create; the filer returns ``None`` on failure
and never propagates.
"""

from __future__ import annotations

import dataclasses
import platform
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from pycastle.display.status_display import StatusDisplay
    from pycastle.managed_worktree_mount_policy import ManagedWorktreeMountRejected
    from pycastle.services import GithubService

BUG_AND_TRIAGE_LABELS: list[str] = ["bug", "needs-triage"]

# ── Credential-redaction helpers (private) ────────────────────────────────────

_CREDENTIAL_KEY_RE = (
    r"(?:api(?:[_ -]?|)key|access(?:[_ -]?|)token|refresh(?:[_ -]?|)token|"
    r"token|secret|password)"
)
_CREDENTIAL_NAMED_VALUE_RE = re.compile(
    rf'(?i)(["\']?{_CREDENTIAL_KEY_RE}["\']?\s*[:=]\s*)(["\']?)([^"\'\s,;}}]+)(\2)'
)
_CREDENTIAL_AFTER_LABEL_RE = re.compile(
    r"(?i)\b(access token|refresh token|api key|token|secret|password)\s+([A-Za-z0-9._:-]{8,})"
)
_SK_STYLE_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _redact_credential_material(text: str) -> str:
    redacted = _CREDENTIAL_NAMED_VALUE_RE.sub(r"\1\2[REDACTED]\4", text)
    redacted = _CREDENTIAL_AFTER_LABEL_RE.sub(r"\1 [REDACTED]", redacted)
    return _SK_STYLE_TOKEN_RE.sub("[REDACTED]", redacted)


# ── Env-block helper ─────────────────────────────────────────────────────────


def _pycastle_version() -> str:
    try:
        return version("pycastle")
    except PackageNotFoundError:
        return "unknown"


def _env_block() -> str:
    """Compose the standard pycastle/Python/OS environment block."""
    py = sys.version_info
    return (
        "## Environment\n"
        f"- pycastle: {_pycastle_version()}\n"
        f"- Python: {py.major}.{py.minor}.{py.micro}\n"
        f"- OS: {platform.platform()}\n"
    )


# ── Report description dataclass ─────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class UpstreamIssueReport:
    """Structured description of an issue to file upstream.

    The ``body`` field should be composed by one of the per-report body
    composers below; the filer prepends the env block before filing.

    Set ``status_display`` to route the "Filed issue" confirmation through
    ``StatusDisplay.print(caller, ...)`` instead of the default
    ``click.echo(...)``.
    """

    dedupe_key: str
    title: str
    body: str
    labels: list[str]
    github_svc: GithubService
    status_display: StatusDisplay | None = None
    caller: str = ""


# ── Filer function ────────────────────────────────────────────────────────────


def file_upstream_issue(report: UpstreamIssueReport) -> int | None:
    """File an issue described by *report*, deduping by ``report.dedupe_key``.

    Searches ``report.github_svc`` for an open issue whose title matches
    ``report.dedupe_key``; returns its number if found.  Otherwise prepends the
    env block to ``report.body``, creates a new issue on ``report.github_svc``
    with ``report.title``, ``report.labels``, and the full body, emits a
    confirmation line, and returns the new issue number.

    Returns ``None`` when ``create_issue_in`` raises ``GithubServiceError``.
    ``GithubServiceError`` from the title search is swallowed and treated as
    "no existing match".  Other exceptions from either call propagate unchanged.
    """
    from pycastle.services import GithubServiceError

    github_svc = report.github_svc

    try:
        existing = github_svc.search_open_issues_by_title(report.dedupe_key)
    except GithubServiceError:
        existing = []

    if existing:
        return existing[0]

    full_body = _env_block() + "\n" + report.body

    try:
        number, _ = github_svc.create_issue_in(
            github_svc.repo, report.title, full_body, report.labels
        )
        message = f"Filed issue #{number} on {github_svc.repo}: {report.title}"
        if report.status_display is not None:
            report.status_display.print(report.caller, message)
        else:
            click.echo(message)
    except GithubServiceError:
        return None
    else:
        return number


# ── Per-report body composers ─────────────────────────────────────────────────
# Each function returns a body string without the env block.  The filer
# prepends the env block before filing.


def hard_agent_error_body(
    *,
    raw: str,
    effective_status_code: int | None,
    caller: str,
    service_name: str,
) -> str:
    """Body for a hard agent-API error report."""
    return (
        f"## Raw result envelope\n\n```json\n{raw}\n```\n\n"
        f"Status: {effective_status_code}\n"
        f"Agent: {caller or '<unknown>'}\n"
        f"Service: {service_name}\n"
    )


def aborted_setup_body(
    *,
    phase: str,
    message: str,
    command: str | None = None,
    output: str | None = None,
) -> str:
    """Body for an aborted-setup failure report."""
    body_parts = [
        "## Setup phase failure\n",
        f"Phase: {phase}\n",
        f"```\n{message}\n```\n",
    ]
    if command:
        body_parts.append(f"Command: `{command}`\n")
    if output:
        body_parts.append(f"Output:\n\n```\n{output}\n```\n")
    return "\n".join(body_parts)


def merge_close_failure_body(*, issue_number: int, exc: BaseException) -> str:
    """Body for a merge-close-failure report."""
    return (
        f"## Merge close failure: issue #{issue_number} could not be closed after merge\n\n"
        f"### Error\n\n```\n{exc}\n```\n\n"
    )


def operator_actionable_body(*, op: str, stderr: str, attempt_count: int) -> str:
    """Body for an operator-actionable git-remote-unreachable report."""
    return (
        f"## git remote unreachable: `{op}` failed after {attempt_count} attempt(s)\n\n"
        f"### Last stderr\n\n```\n{stderr}\n```\n\n"
        f"### Troubleshooting hints\n\n"
        f"- Check your SSH key or HTTPS credentials are valid for the remote.\n"
        f"- Verify the remote URL with `git remote get-url origin`.\n"
        f"- Confirm network connectivity to the remote host.\n\n"
    )


def unrepairable_draft_body(*, problems: list[str], draft_files: dict[str, str]) -> str:
    """Body for an unrepairable improve draft-set report."""
    problems_text = "\n".join(f"- {p}" for p in problems)
    files_section = "\n\n".join(
        f"### `{name}`\n\n```\n{content}\n```"
        for name, content in sorted(draft_files.items())
    )
    return (
        f"## Improve draft set could not be repaired\n\n"
        f"### Validation problems\n\n{problems_text}\n\n"
        f"### Draft file contents\n\n{files_section}\n\n"
    )


def agent_credential_failure_body(
    *,
    service_name: str,
    role_name: str,
    status_code: int | None,
    raw_result_envelope: str,
    remediation: str,
    observations: tuple[tuple[str, str], ...],
) -> str:
    """Body for an operator-actionable agent credential failure report.

    Redacts credential material from observations and the raw result envelope
    before composing the body.
    """
    redacted_observations = tuple(
        (source_stream, _redact_credential_material(raw_text))
        for source_stream, raw_text in observations
    )
    observation_blocks = "\n\n".join(
        f"### {source_stream}\n\n```\n{raw_text}\n```"
        for source_stream, raw_text in redacted_observations
    )
    return (
        "Repair local agent credentials/account access and rerun pycastle.\n\n"
        "This issue is about local agent-provider credentials/account access, "
        "not a source-code defect in the consuming project.\n\n"
        "## Operator-actionable agent credential failure\n\n"
        f"{remediation}\n\n"
        f"Service: {service_name}\n"
        f"Agent: {role_name or '<unknown>'}\n"
        f"Status: {status_code}\n\n"
        f"{observation_blocks}\n\n"
        "### Raw result envelope\n\n"
        f"```json\n{_redact_credential_material(raw_result_envelope)}\n```\n\n"
    )


def diagnostic_mount_fallback_body(
    *,
    caller: str,
    diagnostic_role: str,
    role_name: str,
    original_failure_summary: str,
    rejection: ManagedWorktreeMountRejected,
) -> str:
    """Body for a diagnostic-mount-fallback report."""
    return (
        "## Diagnostic fallback\n\n"
        "No diagnostic agent ran.\n\n"
        f"Pycastle skipped `{caller}` because the managed worktree mount "
        "preconditions were invalid before provider setup.\n\n"
        f"- Role: {role_name}\n"
        f"- Diagnostic role: {diagnostic_role}\n"
        f"- Expected mount path: {rejection.expected_mount_path}\n"
        f"- Provided mount path: {rejection.mount_path}\n"
        f"- Expected worktrees dir: {rejection.expected_worktrees_dir}\n"
        f"- Reason: {rejection.rejection_code}\n"
        f"- Rejection detail: {rejection.detail}\n\n"
        "## Original failure summary\n\n"
        f"{original_failure_summary}\n"
    )
