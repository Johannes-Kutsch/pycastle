from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pycastle.config import Config
from pycastle.errors import (
    ClaudeCliNotFoundError,
    ConfigValidationError,
    DockerBuildError,
    DockerServiceError,
)


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
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            CliRunner().invoke(cli, ["build"])

    assert fake_svc.build_image.call_args[0][0] == "custom-img"


# ── Issue 757: CLI shim translates build_command outcomes to exit codes ──────


def test_build_cmd_exits_zero_on_success(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = None

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 0
    assert "Build complete" in result.output


def test_build_cmd_exits_one_on_docker_service_error(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build_image.side_effect = DockerServiceError("docker not found")

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 1
    assert "docker not found" in result.output


def test_build_cmd_exits_one_on_docker_build_error(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build_image.side_effect = DockerBuildError("build failed")

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 1
    assert "build failed" in result.output


def test_build_cmd_exits_one_when_docker_image_name_is_empty(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="")

    with patch("pycastle.main.load_config", return_value=cfg):
        result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 1
    assert "docker_image_name" in result.output
    assert "pycastle init" in result.output


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
    monkeypatch.setattr(
        "pycastle.commands.labels.click.prompt", lambda *a, **kw: "owner/repo"
    )
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm", lambda *a, **kw: False
    )

    fake_github = MagicMock()
    fake_github.create_label.side_effect = lambda body: posted.append(body)

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.labels.GithubService", return_value=fake_github):
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
    monkeypatch.setattr(
        "pycastle.commands.labels.click.prompt", lambda *a, **kw: "owner/repo"
    )
    monkeypatch.setattr(
        "pycastle.commands.labels.click.confirm", lambda *a, **kw: False
    )
    fake_github = MagicMock()
    monkeypatch.setattr(
        "pycastle.commands.labels.GithubService", lambda *a, **kw: fake_github
    )

    result = CliRunner().invoke(cli, ["labels"])

    assert "Config: defaults" in result.output


def test_build_cmd_prints_layer_summary_at_startup(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    fake_svc = MagicMock()
    with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
        result = CliRunner().invoke(cli, ["build"])

    assert "Config: defaults" in result.output


def test_build_cmd_layer_summary_includes_local_when_present(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("max_parallel = 2\n")

    fake_svc = MagicMock()
    with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
        result = CliRunner().invoke(cli, ["build"])

    assert "Config: defaults + pycastle/config.py" in result.output


def test_init_cmd_prints_layer_summary_at_startup(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    fake_init = MagicMock()
    with patch("pycastle.commands.init.main", fake_init):
        result = CliRunner().invoke(cli, ["init", "--local"])

    assert "Config: defaults" in result.output


# ── Issue 504/691: service registry seeding from env ─────────────────────────


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
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = None

    async def _fake_run(env, repo_root, **kwargs):
        captured["env"] = env
        captured["registry"] = kwargs.get("service_registry")

    with (
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    registry = captured["registry"]
    assert registry is not None
    svc = registry["claude"]
    assert svc.account_names() == ["primary"]
    env = svc.build_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "primary-tok"


def test_run_cmd_seeds_pool_with_secondary_first_when_present(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "primary-tok")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "secondary-tok")
    monkeypatch.setenv("GH_TOKEN", "gh")

    captured: dict = {}
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = None

    async def _fake_run(env, repo_root, **kwargs):
        captured["env"] = env
        captured["registry"] = kwargs.get("service_registry")

    with (
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    registry = captured["registry"]
    svc = registry["claude"]
    assert svc.account_names() == ["secondary", "primary"]
    env = svc.build_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "secondary-tok"


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
    import dataclasses

    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    if not cfg.docker_image_name:
        cfg = dataclasses.replace(cfg, docker_image_name="img")

    captured: dict = {}
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = None

    async def _fake_run(env, repo_root, **kwargs):
        captured["improve_mode"] = kwargs.get("improve_mode")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
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


# ── Issue 796: --no-improve flag ──────────────────────────────────────────────


def test_run_cmd_no_improve_overrides_config(tmp_path, monkeypatch):
    mode = _run_cmd_capturing_improve_mode(
        tmp_path,
        monkeypatch,
        ["--no-improve"],
        Config(improve_mode="until_sleep"),
    )
    assert mode is None


def test_run_cmd_improve_and_no_improve_are_mutually_exclusive(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")

    cfg = Config(docker_image_name="img")

    async def _fake_run(*args, **kwargs):
        pass

    fake_svc = MagicMock()
    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run", "--improve", "--no-improve"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


# ── Issue 759: pycastle run triggers build before orchestrator ────────────────


def _run_cmd_with_build_outcome(tmp_path, monkeypatch, outcome):
    """Invoke run_cmd with a fake build_image returning outcome; return CliRunner result."""

    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = outcome

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        return CliRunner().invoke(cli, ["run"])


def test_run_cmd_triggers_docker_build_before_orchestrator(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    call_order: list[str] = []

    fake_svc = MagicMock()
    fake_svc.build_image.side_effect = lambda *a, **kw: call_order.append("build")

    async def _fake_run(*args, **kwargs):
        call_order.append("orchestrator")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    assert "build" in call_order
    assert "orchestrator" in call_order
    assert call_order.index("build") < call_order.index("orchestrator")


def test_run_cmd_prints_image_up_to_date_on_full_cache_hit(tmp_path, monkeypatch):
    from pycastle.services.docker_service import BuildOutcome

    result = _run_cmd_with_build_outcome(
        tmp_path, monkeypatch, BuildOutcome.FULL_CACHE_HIT
    )

    assert result.exit_code == 0, result.output
    assert "Image up to date" in result.output


def test_run_cmd_no_build_output_on_full_cache_hit(tmp_path, monkeypatch):
    from pycastle.services.docker_service import BuildOutcome

    result = _run_cmd_with_build_outcome(
        tmp_path, monkeypatch, BuildOutcome.FULL_CACHE_HIT
    )

    assert "Build complete" not in result.output


def test_run_cmd_exits_one_when_build_fails(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    fake_svc = MagicMock()
    fake_svc.build_image.side_effect = DockerBuildError("build failed: exit 1")

    orchestrator_called = []

    async def _fake_run(*args, **kwargs):
        orchestrator_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "build failed" in result.output
    assert not orchestrator_called


def test_run_cmd_exits_one_when_docker_image_name_is_empty(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="")

    with patch("pycastle.main.load_config", return_value=cfg):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "docker_image_name" in result.output
    assert "pycastle init" in result.output


def test_run_cmd_does_not_invoke_docker_when_image_name_empty(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="")
    fake_svc = MagicMock()
    orchestrator_called = []

    async def _fake_run(*args, **kwargs):
        orchestrator_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        CliRunner().invoke(cli, ["run"])

    fake_svc.build_image.assert_not_called()
    assert not orchestrator_called


def test_run_cmd_passes_python_version_from_file_to_build(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)
    (tmp_path / ".python-version").write_text("3.12.1\n")

    cfg = Config(docker_image_name="myimage")
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = None

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    call_kwargs = fake_svc.build_image.call_args
    assert call_kwargs.kwargs.get("python_version") == "3.12"


def test_run_cmd_build_uses_streaming_mode(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = None

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    call_kwargs = fake_svc.build_image.call_args
    assert call_kwargs.kwargs.get("stream") is True


def test_run_cmd_rejects_no_cache_flag(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["run", "--no-cache"])

    assert result.exit_code != 0


def test_run_cmd_rejects_no_build_flag(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["run", "--no-build"])

    assert result.exit_code != 0


# ── Issue 760: rebuild status display ────────────────────────────────────────


def _run_cmd_simulating_rebuild(tmp_path, monkeypatch):
    """Invoke run_cmd with a DockerService that signals a rebuild via on_rebuild_start."""
    from pycastle.main import main as cli
    from pycastle.services.docker_service import BuildOutcome

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    fake_svc = MagicMock()

    def _build_with_rebuild(*args, **kwargs):
        cb = kwargs.get("on_rebuild_start")
        if cb:
            cb()
        return BuildOutcome.REBUILT

    fake_svc.build_image.side_effect = _build_with_rebuild

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        return CliRunner().invoke(cli, ["run"])


def test_run_cmd_prints_rebuilding_message_on_cache_miss(tmp_path, monkeypatch):
    result = _run_cmd_simulating_rebuild(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "Rebuilding image" in result.output


def test_run_cmd_no_rebuilding_message_on_full_cache_hit(tmp_path, monkeypatch):
    from pycastle.services.docker_service import BuildOutcome

    result = _run_cmd_with_build_outcome(
        tmp_path, monkeypatch, BuildOutcome.FULL_CACHE_HIT
    )

    assert result.exit_code == 0, result.output
    assert "Rebuilding image" not in result.output


# ── Issue 787: fail-fast service+effort validation ────────────────────────────


def test_run_cmd_exits_nonzero_on_unknown_service_before_docker_build(
    tmp_path, monkeypatch
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="codez"),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build_image.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "codez" in result.output
    assert "plan" in result.output
    assert not build_called


def test_run_cmd_exits_nonzero_on_invalid_effort_for_codex_stage(tmp_path, monkeypatch):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        implement_override=StageOverride(service="codex", effort="max"),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build_image.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "implement" in result.output
    assert "max" in result.output
    assert "codex" in result.output
    assert not build_called


def test_run_cmd_reports_all_violations_in_single_message(tmp_path, monkeypatch):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="codez"),
        implement_override=StageOverride(service="codex", effort="max"),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build_image.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "codez" in result.output
    assert "max" in result.output
    assert not build_called


def test_run_cmd_valid_config_passes_validation_silently(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build_image.return_value = None

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.iteration.orchestrator.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    assert "Config validation" not in result.output
