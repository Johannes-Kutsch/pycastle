"""Auto bug reporter — GH_TOKEN API path with prefilled-URL fallback.

When an unhandled exception escapes a `pycastle` subcommand we print the full
traceback to stderr, then either file a GitHub issue via the API (when
`auto_file_bugs=True` and a `GH_TOKEN` is reachable) or fall through to a
prefilled `https://github.com/{repo}/issues/new?...` URL the user can click.

The reporter must never raise from inside itself — a bug reporter that
crashes on the bug report is the worst outcome.
"""

from __future__ import annotations

import os
import platform
import sys
import traceback
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import quote

from .config import Config

BUG_REPORT_REPO = "Johannes-Kutsch/pycastle"
BUG_REPORT_LABELS = "bug,needs-triage"
BUG_REPORT_LABEL_LIST = ["bug", "needs-triage"]

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


def _safe_load_config() -> Config | None:
    try:
        from .config import load_config

        return load_config()
    except Exception:
        return None


def _safe_resolve_token(cfg: Config | None) -> str | None:
    token = os.environ.get("GH_TOKEN")
    if token:
        return token
    if cfg is None:
        return None
    try:
        from .config import load_env, resolve_global_dir

        env = load_env(
            global_dir=resolve_global_dir(None, os.environ),
            local_env_file=cfg.env_file,
            process_env=os.environ,
        )
        return env.get("GH_TOKEN")
    except Exception:
        return None


def _try_api_path(
    title: str, body: str, repo: str, token: str, cfg: Config
) -> tuple[int, str] | None:
    """Attempt to file an issue via the API. Returns (number, html_url) on
    success, None on any failure."""
    try:
        from .services import GithubService

        svc = GithubService(repo, token, cfg)
        number = svc.create_issue_in(repo, title, body, BUG_REPORT_LABEL_LIST)
        html_url = f"https://github.com/{repo}/issues/{number}"
        return number, html_url
    except Exception:
        return None


def report_and_exit(
    exc: BaseException,
    *,
    cfg: Config | None = None,
    token: str | None = None,
) -> None:
    """Print stderr traceback, file/print a bug report, then exit 1.

    If `cfg.auto_file_bugs` is True and a `GH_TOKEN` is reachable, try the API
    path; on any failure fall through silently to the prefilled URL path.
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(tb_text, file=sys.stderr, end="")

    if cfg is None:
        cfg = _safe_load_config()
    if token is None:
        token = _safe_resolve_token(cfg)

    repo = cfg.bug_report_repo if cfg is not None else BUG_REPORT_REPO
    auto = cfg.auto_file_bugs if cfg is not None else True

    if auto and token and cfg is not None:
        title = _format_title(exc)
        body = _format_body(tb_text)
        result = _try_api_path(title, body, repo, token, cfg)
        if result is not None:
            number, html_url = result
            print(f"Filed issue #{number}: {html_url}")
            sys.exit(1)

    print(build_bug_report_url(exc, tb_text, repo))
    sys.exit(1)
