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
BUG_REPORT_LABEL_LIST = ["bug", "needs-triage"]
BUG_REPORT_LABELS = ",".join(BUG_REPORT_LABEL_LIST)

_MAX_URL_LENGTH = 8000  # comfortably under GitHub's ~8192 URL limit
_MAX_TITLE_LENGTH = 200  # GitHub caps issue titles at 256 chars
_TRUNCATION_FOOTER = "\n\n[traceback truncated — see terminal stderr for full output]"


def _pycastle_version() -> str:
    try:
        return version("pycastle")
    except PackageNotFoundError:
        return "unknown"


def _env_block() -> str:
    py = sys.version_info
    return (
        "## Environment\n"
        f"- pycastle: {_pycastle_version()}\n"
        f"- Python: {py.major}.{py.minor}.{py.micro}\n"
        f"- OS: {platform.platform()}\n"
    )


def _format_title(exc: BaseException) -> str:
    msg = str(exc)
    first_line = msg.splitlines()[0] if msg else ""
    title = f"[pycastle] {type(exc).__name__}: {first_line}"
    if len(title) > _MAX_TITLE_LENGTH:
        title = title[: _MAX_TITLE_LENGTH - 1] + "…"
    return title


def _build_url(title: str, body: str, labels_str: str, repo: str) -> str:
    return (
        f"https://github.com/{repo}/issues/new"
        f"?title={quote(title)}"
        f"&body={quote(body)}"
        f"&labels={quote(labels_str)}"
    )


def _build_bug_report_url(
    title: str,
    body: str,
    labels: list[str],
    repo: str,
) -> str:
    """Build a prefilled GitHub `issues/new` URL.

    Prepends the env block to `body`. Truncates the body so the final URL
    stays under GitHub's URL length limit, preserving the env block and
    appending a truncation footer so the report still points the maintainer
    at the terminal stderr.
    """
    env = _env_block()
    label_str = ",".join(labels)
    full_body = env + "\n" + body
    url = _build_url(title, full_body, label_str, repo)
    if len(url) <= _MAX_URL_LENGTH:
        return url

    truncated = body
    while truncated and len(url) > _MAX_URL_LENGTH:
        truncated = truncated[: max(len(truncated) - 200, 0)]
        full_body = env + "\n" + truncated + _TRUNCATION_FOOTER + "\n"
        url = _build_url(title, full_body, label_str, repo)
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


def auto_file_issue(
    title: str,
    body: str,
    labels: list[str],
    *,
    cfg: Config | None,
) -> str:
    """Gate-check, token-resolve, and file (or print prefilled URL for) a GitHub issue.

    Prepends the pycastle/Python/OS environment block to `body`. Never raises.
    Returns the filed issue URL or the prefilled issues/new URL.
    """
    if cfg is None:
        cfg = _safe_load_config()
    token = _safe_resolve_token(cfg)
    repo = cfg.bug_report_repo if cfg is not None else BUG_REPORT_REPO
    full_body = _env_block() + "\n" + body

    if cfg is not None and cfg.auto_file_bugs and token:
        result = _try_api_path(title, full_body, repo, token, cfg)
        if result is not None:
            number, html_url = result
            print(f"Filed issue #{number}: {html_url}")
            return html_url

    url = _build_bug_report_url(title, body, labels, repo)
    print(url)
    return url


def report_and_exit(
    exc: BaseException,
    *,
    cfg: Config | None = None,
) -> None:
    """Print stderr traceback, file/print a bug report, then exit 1."""
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(tb_text, file=sys.stderr, end="")

    auto_file_issue(
        title=_format_title(exc),
        body=f"## Traceback\n```\n{tb_text}\n```\n",
        labels=BUG_REPORT_LABEL_LIST,
        cfg=cfg,
    )
    sys.exit(1)
