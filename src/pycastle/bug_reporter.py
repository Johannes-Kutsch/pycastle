"""Auto bug reporter — prefilled-URL fallback path.

When an unhandled exception escapes a `pycastle` subcommand we print the full
traceback to stderr, then print a `https://github.com/{repo}/issues/new?...`
URL with the title, body, and labels filled in so the user can click through
to file a bug. Slice 1 of the auto bug reporter (issue #501) — no API call,
no token plumbing yet.
"""

from __future__ import annotations

import platform
import sys
import traceback
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import quote

BUG_REPORT_REPO = "Johannes-Kutsch/pycastle"
BUG_REPORT_LABELS = "bug,needs-triage"

_MAX_URL_LENGTH = 8000  # comfortably under GitHub's ~8192 URL limit
_MAX_TITLE_LENGTH = 200  # GitHub caps issue titles at 256 chars
_TRUNCATION_FOOTER = "\n\n[traceback truncated — see terminal stderr for full output]"


def _pycastle_version() -> str:
    try:
        return version("pycastle")
    except PackageNotFoundError:
        return "unknown"


def _format_title(exc: BaseException) -> str:
    msg = str(exc)
    first_line = msg.splitlines()[0] if msg else ""
    title = f"[pycastle] {type(exc).__name__}: {first_line}"
    if len(title) > _MAX_TITLE_LENGTH:
        title = title[: _MAX_TITLE_LENGTH - 1] + "…"
    return title


def _format_body(traceback_text: str) -> str:
    py = sys.version_info
    env_block = (
        "## Environment\n"
        f"- pycastle: {_pycastle_version()}\n"
        f"- Python: {py.major}.{py.minor}.{py.micro}\n"
        f"- OS: {platform.platform()}\n"
    )
    return f"{env_block}\n## Traceback\n```\n{traceback_text}\n```\n"


def _build_url(title: str, body: str, repo: str) -> str:
    return (
        f"https://github.com/{repo}/issues/new"
        f"?title={quote(title)}"
        f"&body={quote(body)}"
        f"&labels={quote(BUG_REPORT_LABELS)}"
    )


def build_bug_report_url(
    exc: BaseException,
    traceback_text: str,
    repo: str = BUG_REPORT_REPO,
) -> str:
    """Build a prefilled GitHub `issues/new` URL for an unhandled exception.

    Truncates the traceback portion of the body so the final URL stays under
    GitHub's URL length limit, leaving the env block and a footer line intact
    so the report still points the maintainer at the terminal stderr.
    """
    title = _format_title(exc)
    url = _build_url(title, _format_body(traceback_text), repo)
    if len(url) <= _MAX_URL_LENGTH:
        return url

    truncated = traceback_text
    while truncated and len(url) > _MAX_URL_LENGTH:
        truncated = truncated[: max(len(truncated) - 200, 0)]
        body = _format_body(truncated) + _TRUNCATION_FOOTER + "\n"
        url = _build_url(title, body, repo)
    return url


def report_and_exit(exc: BaseException) -> None:
    """Print stderr traceback + stdout bug-report URL, then exit 1."""
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(tb_text, file=sys.stderr, end="")
    print(build_bug_report_url(exc, tb_text))
    sys.exit(1)
