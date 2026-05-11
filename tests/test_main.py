from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pycastle.config import Config
from pycastle.errors import ClaudeCliNotFoundError, ConfigValidationError


# ── Issue 203: cfg injection into _load_env ───────────────────────────────────


def test_load_env_reads_keys_from_cfg_env_file(tmp_path, monkeypatch):
    """_load_env(cfg=Config(env_file=...)) must resolve keys from that file."""
    from pycastle.main import _load_env

    custom_env = tmp_path / "custom.env"
    custom_env.write_text("GH_TOKEN=from-custom-file\n")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("PYCASTLE_HOME", raising=False)

    env = _load_env(cfg=Config(env_file=custom_env))

    assert env["GH_TOKEN"] == "from-custom-file"


def test_load_env_returns_only_known_keys(tmp_path, monkeypatch):
    """_load_env returns only known credential keys; never reads host fs."""
    from pycastle.main import _load_env

    custom_env = tmp_path / "custom.env"
    custom_env.write_text("CLAUDE_CODE_OAUTH_TOKEN=oauth-tok\nGH_TOKEN=gh-tok\n")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("PYCASTLE_HOME", raising=False)

    def _no_home() -> None:
        raise AssertionError("_load_env must not read from the host filesystem")

    monkeypatch.setattr("pycastle.main.Path.home", _no_home)

    env = _load_env(cfg=Config(env_file=custom_env))

    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok", "GH_TOKEN": "gh-tok"}


def test_load_env_includes_secondary_oauth_token_when_present(tmp_path, monkeypatch):
    from pycastle.main import _load_env

    custom_env = tmp_path / "custom.env"
    custom_env.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=primary-tok\n"
        "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY=secondary-tok\n"
        "GH_TOKEN=gh-tok\n"
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("PYCASTLE_HOME", raising=False)

    env = _load_env(cfg=Config(env_file=custom_env))

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "primary-tok"
    assert env["CLAUDE_CODE_OAUTH_TOKEN_SECONDARY"] == "secondary-tok"


def test_run_cmd_fails_fast_when_oauth_token_missing(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.output
    assert "claude setup-token" in result.output


# ── Issue 309: load_config() called at entry in CLI commands ──────────────────


def test_build_cmd_exits_one_on_invalid_config(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config", side_effect=ConfigValidationError("bad model")
    ):
        result = CliRunner().invoke(cli, ["build"])
    assert result.exit_code == 1


def test_build_cmd_shows_config_error_message(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config", side_effect=ConfigValidationError("bad model")
    ):
        result = CliRunner().invoke(cli, ["build"])
    assert "bad model" in result.output


def test_labels_cmd_exits_one_on_invalid_config(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config", side_effect=ConfigValidationError("bad effort")
    ):
        result = CliRunner().invoke(cli, ["labels"])
    assert result.exit_code == 1


def test_labels_cmd_shows_config_error_message(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config", side_effect=ConfigValidationError("bad effort")
    ):
        result = CliRunner().invoke(cli, ["labels"])
    assert "bad effort" in result.output


def test_run_cmd_exits_one_on_invalid_config(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config", side_effect=ConfigValidationError("bad model")
    ):
        result = CliRunner().invoke(cli, ["run"])
    assert result.exit_code == 1


def test_run_cmd_shows_config_error_message(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config", side_effect=ConfigValidationError("bad model")
    ):
        result = CliRunner().invoke(cli, ["run"])
    assert "bad model" in result.output


def test_build_cmd_uses_config_docker_image_name(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="custom-img")
    fake_svc = MagicMock()

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.build_command.DockerService", return_value=fake_svc):
            CliRunner().invoke(cli, ["build"])

    assert fake_svc.build_image.call_args[0][0] == "custom-img"


# ── Issue 329: --version flag ─────────────────────────────────────────────────


def test_version_flag_exits_zero():
    from pycastle.main import main as cli

    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0


def test_version_flag_output_contains_pycastle_and_version():
    from pycastle.main import main as cli

    result = CliRunner().invoke(cli, ["--version"])
    assert "pycastle" in result.output
    assert "version" in result.output


def test_labels_cmd_creates_labels_with_config_issue_label(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config(issue_label="custom-ready")
    posted: list = []

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr("pycastle.labels.click.prompt", lambda *a, **kw: "owner/repo")
    monkeypatch.setattr("pycastle.labels.click.confirm", lambda *a, **kw: False)

    fake_github = MagicMock()
    fake_github.create_label.side_effect = lambda body: posted.append(body)

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.labels.GithubService", return_value=fake_github):
            CliRunner().invoke(cli, ["labels"])

    label_names = {entry["name"] for entry in posted}
    assert "custom-ready" in label_names


# ── Issue 330: ClaudeCliNotFoundError shows install instruction ───────────────


def test_run_cmd_exits_one_when_claude_cli_missing(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["run"])
    assert result.exit_code == 1


def test_run_cmd_shows_install_instruction_when_claude_cli_missing(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["run"])
    assert "sudo npm install -g @anthropic-ai/claude-code" in result.output


def test_run_cmd_exits_cleanly_when_claude_cli_missing(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["run"])
    assert isinstance(result.exception, SystemExit)


def test_build_cmd_exits_one_when_claude_cli_missing(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["build"])
    assert result.exit_code == 1


def test_build_cmd_shows_install_instruction_when_claude_cli_missing(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["build"])
    assert "sudo npm install -g @anthropic-ai/claude-code" in result.output


def test_labels_cmd_exits_one_when_claude_cli_missing(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["labels"])
    assert result.exit_code == 1


def test_labels_cmd_shows_install_instruction_when_claude_cli_missing(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["labels"])
    assert "sudo npm install -g @anthropic-ai/claude-code" in result.output


# ── Issue 475: layer summary line ─────────────────────────────────────────


def test_labels_cmd_prints_layer_summary_at_startup(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr("pycastle.labels.click.prompt", lambda *a, **kw: "owner/repo")
    monkeypatch.setattr("pycastle.labels.click.confirm", lambda *a, **kw: False)
    fake_github = MagicMock()
    monkeypatch.setattr("pycastle.labels.GithubService", lambda *a, **kw: fake_github)

    result = CliRunner().invoke(cli, ["labels"])

    assert "Config: defaults" in result.output


def test_build_cmd_prints_layer_summary_at_startup(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    fake_svc = MagicMock()
    with patch("pycastle.build_command.DockerService", return_value=fake_svc):
        result = CliRunner().invoke(cli, ["build"])

    assert "Config: defaults" in result.output


def test_build_cmd_layer_summary_includes_local_when_present(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("max_parallel = 2\n")

    fake_svc = MagicMock()
    with patch("pycastle.build_command.DockerService", return_value=fake_svc):
        result = CliRunner().invoke(cli, ["build"])

    assert "Config: defaults + pycastle/config.py" in result.output


def test_init_cmd_prints_layer_summary_at_startup(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    fake_init = MagicMock()
    with patch("pycastle.init_command.main", fake_init):
        result = CliRunner().invoke(cli, ["init", "--local"])

    assert "Config: defaults" in result.output


# ── Issue 504: AccountPool seeding from env ───────────────────────────────────


def test_run_cmd_seeds_pool_with_primary_only_when_secondary_absent(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "primary-tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    captured: dict = {}

    async def _fake_run(env, repo_root, **kwargs):
        captured["env"] = env
        captured["pool"] = kwargs.get("account_pool")

    with patch("pycastle.orchestrator.run", _fake_run):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    pool = captured["pool"]
    assert pool is not None
    assert pool.names() == ["primary"]
    assert pool.pick() == ("primary", "primary-tok")


def test_run_cmd_seeds_pool_with_secondary_first_when_present(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "primary-tok")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "secondary-tok")
    monkeypatch.setenv("GH_TOKEN", "gh")

    captured: dict = {}

    async def _fake_run(env, repo_root, **kwargs):
        captured["env"] = env
        captured["pool"] = kwargs.get("account_pool")

    with patch("pycastle.orchestrator.run", _fake_run):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    pool = captured["pool"]
    assert pool.names() == ["secondary", "primary"]
    assert pool.pick() == ("secondary", "secondary-tok")


def test_run_cmd_fails_fast_when_primary_token_missing_even_with_secondary(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "secondary-tok")

    result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.output


# ── Issue 670: improve_mode config field / CLI precedence matrix ──────────────


def _run_cmd_capturing_improve_mode(
    tmp_path, monkeypatch, cli_args: list[str], cfg: Config
):
    """Helper: invoke run_cmd and return the improve_mode passed to orchestrator.run."""
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    captured: dict = {}

    async def _fake_run(env, repo_root, **kwargs):
        captured["improve_mode"] = kwargs.get("improve_mode")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"] + cli_args)

    assert result.exit_code == 0, result.output
    return captured["improve_mode"]


def test_run_cmd_improve_mode_absent_flag_absent_config_is_none(tmp_path, monkeypatch):
    mode = _run_cmd_capturing_improve_mode(tmp_path, monkeypatch, [], Config())
    assert mode is None


def test_run_cmd_improve_mode_absent_flag_config_set_uses_config(tmp_path, monkeypatch):
    mode = _run_cmd_capturing_improve_mode(
        tmp_path, monkeypatch, [], Config(improve_mode="until_sleep")
    )
    assert mode == "until_sleep"


def test_run_cmd_improve_mode_flag_set_absent_config_uses_flag(tmp_path, monkeypatch):
    mode = _run_cmd_capturing_improve_mode(
        tmp_path, monkeypatch, ["--improve", "endless"], Config()
    )
    assert mode == "endless"


def test_run_cmd_improve_mode_flag_overrides_config(tmp_path, monkeypatch):
    mode = _run_cmd_capturing_improve_mode(
        tmp_path,
        monkeypatch,
        ["--improve", "endless"],
        Config(improve_mode="until_sleep"),
    )
    assert mode == "endless"


def test_run_cmd_improve_mode_bare_flag_defaults_to_until_sleep(tmp_path, monkeypatch):
    mode = _run_cmd_capturing_improve_mode(
        tmp_path, monkeypatch, ["--improve"], Config()
    )
    assert mode == "until_sleep"
