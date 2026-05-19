"""Tests for issue #501: auto bug reporter MVP — prefilled-URL fallback path."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import click
import pytest
from click.testing import CliRunner


# ── Helpers ───────────────────────────────────────────────────────────────────


def _install_crashing_subcommand(monkeypatch, exc: BaseException) -> None:
    """Replace one of the real subcommand bodies so it raises `exc`."""

    def _boom(*_a, **_kw):
        raise exc

    monkeypatch.setattr("pycastle.main._load_config_or_exit", _boom)


def _find_url_in_output(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("https://github.com/Johannes-Kutsch/pycastle/issues/new"):
            return line
    raise AssertionError(f"no bug report URL in output:\n{output}")


# ── Tracer bullet: end-to-end URL is printed ──────────────────────────────────


def test_unhandled_exception_prints_prefilled_url(monkeypatch):
    from pycastle.main import main as cli

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])

    url = _find_url_in_output(result.stdout)
    parsed = urlparse(url)
    assert parsed.netloc == "github.com"
    assert parsed.path == "/Johannes-Kutsch/pycastle/issues/new"


def test_unhandled_exception_exits_one(monkeypatch):
    from pycastle.main import main as cli

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 1


def test_url_title_uses_exception_class_and_first_line(monkeypatch):
    from pycastle.main import main as cli

    _install_crashing_subcommand(monkeypatch, ValueError("first line\nsecond line"))
    result = CliRunner().invoke(cli, ["build"])

    url = _find_url_in_output(result.stdout)
    qs = parse_qs(urlparse(url).query)
    assert qs["title"] == ["[pycastle] ValueError: first line"]


def test_url_labels_are_bug_and_needs_triage(monkeypatch):
    from pycastle.main import main as cli

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])

    url = _find_url_in_output(result.stdout)
    qs = parse_qs(urlparse(url).query)
    assert qs["labels"] == ["bug,needs-triage"]


def test_url_body_contains_environment_and_traceback(monkeypatch):
    from pycastle.main import main as cli

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom-marker"))
    result = CliRunner().invoke(cli, ["build"])

    url = _find_url_in_output(result.stdout)
    body = parse_qs(urlparse(url).query)["body"][0]
    assert "## Environment" in body
    assert "pycastle:" in body
    assert "Python:" in body
    assert "OS:" in body
    assert "## Traceback" in body
    assert "RuntimeError: boom-marker" in body


def test_full_traceback_printed_to_stderr(monkeypatch):
    from pycastle.main import main as cli

    _install_crashing_subcommand(monkeypatch, RuntimeError("stderr-marker"))
    result = CliRunner().invoke(cli, ["build"])

    assert "Traceback" in result.stderr
    assert "RuntimeError: stderr-marker" in result.stderr


# ── Truncation: long traceback ────────────────────────────────────────────────


def test_long_traceback_url_stays_under_github_limit(monkeypatch):
    from pycastle.main import main as cli

    huge = RuntimeError("x" * 50_000)
    _install_crashing_subcommand(monkeypatch, huge)
    result = CliRunner().invoke(cli, ["build"])

    url = _find_url_in_output(result.stdout)
    assert len(url) < 8192


def test_long_traceback_body_has_truncation_footer(monkeypatch):
    from pycastle.main import main as cli

    huge = RuntimeError("x" * 50_000)
    _install_crashing_subcommand(monkeypatch, huge)
    result = CliRunner().invoke(cli, ["build"])

    url = _find_url_in_output(result.stdout)
    body = parse_qs(urlparse(url).query)["body"][0]
    assert "[traceback truncated — see terminal stderr for full output]" in body


def test_long_traceback_full_text_still_on_stderr(monkeypatch):
    from pycastle.main import main as cli

    needle = "needle-" + "x" * 49_000
    _install_crashing_subcommand(monkeypatch, RuntimeError(needle))
    result = CliRunner().invoke(cli, ["build"])

    assert needle in result.stderr


# ── Click flow control passes through unchanged ───────────────────────────────


def test_click_usage_error_passes_through_without_url(monkeypatch):
    from pycastle.main import main as cli

    def _boom(*_a, **_kw):
        raise click.UsageError("bad usage")

    monkeypatch.setattr("pycastle.main._load_config_or_exit", _boom)
    result = CliRunner().invoke(cli, ["build"])

    assert "issues/new" not in result.stdout
    assert result.exit_code == 2  # click default for UsageError


def test_click_abort_passes_through_without_url(monkeypatch):
    from pycastle.main import main as cli

    def _boom(*_a, **_kw):
        raise click.Abort()

    monkeypatch.setattr("pycastle.main._load_config_or_exit", _boom)
    result = CliRunner().invoke(cli, ["build"])

    assert "issues/new" not in result.stdout


def test_unknown_subcommand_does_not_invoke_reporter():
    from pycastle.main import main as cli

    result = CliRunner().invoke(cli, ["no-such-cmd"])

    assert "issues/new" not in result.stdout
    assert result.exit_code != 0


# ── Reporter wired uniformly across subcommands ───────────────────────────────


@pytest.mark.parametrize("subcmd", ["build", "labels", "run"])
def test_reporter_fires_for_each_subcommand(monkeypatch, subcmd):
    from pycastle.main import main as cli

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, [subcmd])

    assert "issues/new" in result.stdout
    assert result.exit_code == 1


# ── Repo target lives in a single named constant ──────────────────────────────


def test_repo_target_is_module_level_constant():
    from pycastle import bug_reporter

    assert bug_reporter.BUG_REPORT_REPO == "Johannes-Kutsch/pycastle"


# ── API path: GH_TOKEN + auto_file_bugs gating ────────────────────────────────


def test_auto_file_bugs_false_with_token_uses_url_path(monkeypatch):
    from pycastle import bug_reporter
    from pycastle.config import Config
    from pycastle.main import main as cli

    monkeypatch.setenv("GH_TOKEN", "tkn")
    monkeypatch.setattr(
        "pycastle.bug_reporter._safe_load_config",
        lambda: Config(auto_file_bugs=False),
    )

    def _should_not_be_called(*a, **kw):
        raise AssertionError("API path should not run when auto_file_bugs=False")

    monkeypatch.setattr(bug_reporter, "_try_api_path", _should_not_be_called)

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])
    _find_url_in_output(result.stdout)  # asserts URL present
    assert result.exit_code == 1


def test_auto_file_bugs_true_no_token_uses_url_path(monkeypatch):
    from pycastle import bug_reporter
    from pycastle.main import main as cli

    # GH_TOKEN already cleared by autouse fixture
    def _should_not_be_called(*a, **kw):
        raise AssertionError("API path should not run without a token")

    monkeypatch.setattr(bug_reporter, "_try_api_path", _should_not_be_called)

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])
    _find_url_in_output(result.stdout)
    assert result.exit_code == 1


def test_auto_file_bugs_true_with_token_and_200_uses_api_path(monkeypatch):
    from pycastle import bug_reporter
    from pycastle.config import Config
    from pycastle.main import main as cli

    monkeypatch.setenv("GH_TOKEN", "tkn")
    monkeypatch.setattr(
        "pycastle.bug_reporter._safe_load_config",
        lambda: Config(auto_file_bugs=True),
    )
    monkeypatch.setattr(
        bug_reporter,
        "_try_api_path",
        lambda title, body, repo, token, cfg: (
            42,
            "https://github.com/Johannes-Kutsch/pycastle/issues/42",
        ),
    )

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])

    assert "Filed issue #42:" in result.stdout
    assert "https://github.com/Johannes-Kutsch/pycastle/issues/42" in result.stdout
    assert "issues/new" not in result.stdout
    assert result.exit_code == 1


def test_api_path_503_falls_through_to_url(monkeypatch):
    from pycastle.config import Config
    from pycastle.main import main as cli

    monkeypatch.setenv("GH_TOKEN", "tkn")
    monkeypatch.setattr(
        "pycastle.bug_reporter._safe_load_config",
        lambda: Config(auto_file_bugs=True),
    )

    from pycastle.services import GithubAPIError

    def _boom_api(self, owner_repo, title, body, labels):
        raise GithubAPIError("500", status=503, body="down", method="POST", path="/x")

    monkeypatch.setattr("pycastle.services.GithubService.create_issue_in", _boom_api)

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])

    _find_url_in_output(result.stdout)  # URL fallback printed
    assert result.exit_code == 1


def test_api_path_network_error_falls_through_to_url(monkeypatch):
    from pycastle.config import Config
    from pycastle.main import main as cli
    from pycastle.services import GithubNetworkError

    monkeypatch.setenv("GH_TOKEN", "tkn")
    monkeypatch.setattr(
        "pycastle.bug_reporter._safe_load_config",
        lambda: Config(auto_file_bugs=True),
    )

    def _boom_net(self, owner_repo, title, body, labels):
        raise GithubNetworkError("dns fail", cause=OSError("dns"))

    monkeypatch.setattr("pycastle.services.GithubService.create_issue_in", _boom_net)

    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])

    _find_url_in_output(result.stdout)
    assert result.exit_code == 1


def test_url_uses_cfg_bug_report_repo(monkeypatch):
    from pycastle import bug_reporter
    from pycastle.config import Config
    from pycastle.main import main as cli

    monkeypatch.setattr(
        bug_reporter,
        "_safe_load_config",
        lambda: Config(bug_report_repo="other-owner/other-repo"),
    )
    _install_crashing_subcommand(monkeypatch, RuntimeError("boom"))
    result = CliRunner().invoke(cli, ["build"])

    assert "https://github.com/other-owner/other-repo/issues/new" in result.stdout
