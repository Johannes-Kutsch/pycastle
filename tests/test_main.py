import pytest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pycastle._universal_image_build import (
    UniversalImageBuildRequest,
)
from pycastle.config import Config, StageOverride
from pycastle.errors import (
    ClaudeCliNotFoundError,
    ConfigValidationError,
    DockerBuildError,
    DockerServiceError,
)


def _built_requests(fake_svc: MagicMock) -> list[UniversalImageBuildRequest]:
    return [call.args[0] for call in fake_svc.build.call_args_list]


# ── Issue 203: cfg injection into _load_env ───────────────────────────────────


def test_load_env_uses_fixed_project_local_env_file_with_global_layering(
    tmp_path, monkeypatch
):
    """_load_env() must layer global .env under the fixed local pycastle/.env, ignoring stale env_file config."""
    from pycastle.main import _load_env

    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=from-global\n")
    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / ".env").write_text("GH_TOKEN=from-local-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(global_dir))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    env = _load_env(cfg=Config())

    assert env["GH_TOKEN"] == "from-local-file"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "from-global"


def test_load_env_returns_only_known_keys_with_isolated_global_layer(
    tmp_path, monkeypatch
):
    """_load_env returns only known credential keys from isolated test-owned env layers."""
    from pycastle.main import _load_env

    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=oauth-tok\nGH_TOKEN=gh-tok\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    def _no_home() -> None:
        raise AssertionError("_load_env must not read from the host filesystem")

    monkeypatch.setattr("pycastle.main.Path.home", _no_home)

    env = _load_env(cfg=Config())

    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok", "GH_TOKEN": "gh-tok"}


def test_load_env_includes_main_claude_oauth_token_when_present(tmp_path, monkeypatch):
    from pycastle.main import _load_env

    pycastle_dir = tmp_path / "pycastle"
    pycastle_dir.mkdir()
    (pycastle_dir / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=primary-tok\n"
        "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY=secondary-tok\n"
        "GH_TOKEN=gh-tok\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)
    monkeypatch.delenv("PYCASTLE_HOME", raising=False)

    env = _load_env(cfg=Config())

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "primary-tok"


def test_run_cmd_dispatches_via_pycastle_owned_orchestration_adapter(
    tmp_path, monkeypatch
):
    from pycastle import orchestration
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    captured: dict[str, object] = {}

    async def _fake_run(env, repo_root, **kwargs):
        captured["env"] = env
        captured["repo_root"] = repo_root
        captured["service_registry"] = kwargs.get("service_registry")
        captured["improve_mode"] = kwargs.get("improve_mode")

    with (
        patch("pycastle.commands.build.main"),
        patch.object(orchestration, "run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    assert captured["repo_root"] == tmp_path
    assert captured["service_registry"] is not None
    assert captured["improve_mode"] is None


def test_run_cmd_fails_clearly_when_stage_chain_has_no_locally_configured_service(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    absent_chain = StageOverride(
        service="claude",
        model="sonnet",
        effort="medium",
        fallback=StageOverride(service="opencode", model="kimi-k2.6", effort="medium"),
    )
    codex_only = StageOverride(service="codex", model="gpt-5.4", effort="medium")
    cfg = Config(
        docker_image_name="img",
        plan_override=codex_only,
        implement_override=absent_chain,
        review_override=codex_only,
        merge_override=codex_only,
        preflight_issue_override=codex_only,
        improve_override=codex_only,
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert (
        "stage='implement': no locally configured service in priority chain"
        in result.output
    )
    assert "claude -> opencode" in result.output
    assert not build_called


def test_run_cmd_preserves_full_stage_priority_chain_label_for_unconfigured_services(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    absent_chain = StageOverride(
        service="claude",
        model="sonnet",
        effort="medium",
        fallback=StageOverride(
            service="opencode",
            model="kimi-k2.6",
            effort="medium",
            fallback=StageOverride(
                service="claude",
                model="haiku",
                effort="medium",
            ),
        ),
    )
    codex_only = StageOverride(service="codex", model="gpt-5.4", effort="medium")
    cfg = Config(
        docker_image_name="img",
        plan_override=codex_only,
        implement_override=absent_chain,
        review_override=codex_only,
        merge_override=codex_only,
        preflight_issue_override=codex_only,
        improve_override=codex_only,
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert (
        "stage='implement': no locally configured service in priority chain"
        in result.output
    )
    assert "claude -> opencode -> claude" in result.output
    assert not build_called


def test_run_cmd_rejects_empty_stage_override_service_before_credentials(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img", plan_override=StageOverride(service="", effort="low")
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "stage='plan': service is required" in result.output
    assert "CLAUDE_CODE_OAUTH_TOKEN is not set" not in result.output
    assert not build_called


def test_run_cmd_rejects_empty_stage_override_effort_before_credentials(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        review_override=StageOverride(service="claude", effort=""),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "stage='review': effort is required" in result.output
    assert "CLAUDE_CODE_OAUTH_TOKEN is not set" not in result.output
    assert not build_called


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

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config", side_effect=ConfigValidationError("bad model")
    ):
        result = CliRunner().invoke(cli, ["run"])
    assert "bad model" in result.output


def test_build_cmd_uses_config_docker_image_name(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="custom-img")
    fake_svc = MagicMock()

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            CliRunner().invoke(cli, ["build"])

    assert [request.image_tag for request in _built_requests(fake_svc)] == [
        "custom-img"
    ]


# ── Issue 757: CLI shim translates build_command outcomes to exit codes ──────


def test_build_cmd_exits_zero_on_success(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build.return_value = None

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 0
    assert "Build complete" in result.output


def test_build_cmd_exits_one_on_docker_service_error(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build.side_effect = DockerServiceError("docker not found")

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 1
    assert "docker not found" in result.output


def test_build_cmd_exits_one_on_docker_build_error(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build.side_effect = DockerBuildError("build failed")

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.commands.build.DockerService", return_value=fake_svc):
            result = CliRunner().invoke(cli, ["build"])

    assert result.exit_code == 1
    assert "build failed" in result.output


def test_build_cmd_exits_one_when_docker_image_name_is_empty(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    with patch(
        "pycastle.main.load_config",
        side_effect=ClaudeCliNotFoundError("claude not found"),
    ):
        result = CliRunner().invoke(cli, ["run"])
    assert "sudo npm install -g @anthropic-ai/claude-code" in result.output


def test_run_cmd_exits_cleanly_when_claude_cli_missing(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
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


# ── Issue 670: improve_mode config field / CLI precedence matrix ──────────────


def _run_cmd_capturing_improve_mode(
    tmp_path, monkeypatch, cli_args: list[str], cfg: Config
):
    """Helper: invoke run_cmd and return the improve_mode passed to the runtime entrypoint."""
    import dataclasses

    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    if not cfg.docker_image_name:
        cfg = dataclasses.replace(cfg, docker_image_name="img")

    captured: dict = {}
    fake_svc = MagicMock()
    fake_svc.build.return_value = None

    async def _fake_run(env, repo_root, **kwargs):
        captured["improve_mode"] = kwargs.get("improve_mode")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.main.agent_runtime.run", _fake_run),
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

    (tmp_path / "pycastle").mkdir()
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
        patch("pycastle.main.agent_runtime.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run", "--improve", "--no-improve"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


# ── Issue 759: pycastle run triggers build before orchestrator ────────────────


def _run_cmd_with_build_outcome(tmp_path, monkeypatch, outcome):
    """Invoke run_cmd with a fake typed build request returning outcome; return CliRunner result."""

    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    fake_svc = MagicMock()
    fake_svc.build.return_value = outcome

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.main.agent_runtime.run", _fake_run),
    ):
        return CliRunner().invoke(cli, ["run"])


def test_run_cmd_triggers_docker_build_before_orchestrator(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    call_order: list[str] = []

    async def _fake_run(*args, **kwargs):
        call_order.append("orchestrator")

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch(
            "pycastle.commands.build.main",
            side_effect=lambda **kwargs: call_order.append("build"),
        ),
        patch("pycastle.main.agent_runtime.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    assert "build" in call_order
    assert "orchestrator" in call_order
    assert call_order.index("build") < call_order.index("orchestrator")


def test_run_cmd_exits_one_when_build_fails(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="myimage")
    orchestrator_called = []

    async def _fake_run(*args, **kwargs):
        orchestrator_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch(
            "pycastle.commands.build.main",
            side_effect=DockerBuildError("build failed: exit 1"),
        ),
        patch("pycastle.main.agent_runtime.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "build failed" in result.output
    assert not orchestrator_called


def test_run_cmd_exits_one_when_docker_image_name_is_empty(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
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

    (tmp_path / "pycastle").mkdir()
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
        patch("pycastle.main.agent_runtime.run", _fake_run),
    ):
        CliRunner().invoke(cli, ["run"])

    fake_svc.build.assert_not_called()
    assert not orchestrator_called


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


# ── Issue 787: fail-fast service+effort validation ────────────────────────────


def test_run_cmd_exits_nonzero_on_unknown_service_before_docker_build(
    tmp_path, monkeypatch
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
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
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

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

    (tmp_path / "pycastle").mkdir()
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
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

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


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_run_cmd_exits_nonzero_on_unsupported_codex_effort_for_stage(
    tmp_path, monkeypatch, effort
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        implement_override=StageOverride(service="codex", effort=effort),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "implement" in result.output
    assert effort in result.output
    assert "codex" in result.output
    assert "low" in result.output
    assert "medium" in result.output
    assert "high" in result.output
    assert "xhigh" in result.output
    assert not build_called


@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_run_cmd_exits_nonzero_on_unsupported_codex_effort_for_stage_fallback(
    tmp_path, monkeypatch, effort
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        implement_override=StageOverride(
            service="claude",
            effort="medium",
            fallback=StageOverride(service="codex", effort=effort),
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "implement fallback" in result.output
    assert effort in result.output
    assert "codex" in result.output
    assert "low" in result.output
    assert "medium" in result.output
    assert "high" in result.output
    assert "xhigh" in result.output
    assert not build_called


def test_run_cmd_exits_nonzero_on_invalid_effort_for_claude_stage(
    tmp_path, monkeypatch
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="claude", effort="ultra"),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "plan" in result.output
    assert "ultra" in result.output
    assert not build_called


def test_run_cmd_exits_nonzero_on_invalid_claude_model_with_suggestion(
    tmp_path, monkeypatch
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="claude", model="sonnnet", effort="low"),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "plan" in result.output
    assert "sonnnet" in result.output
    assert 'Did you mean "sonnet"?' in result.output
    assert not build_called


def test_run_cmd_exits_nonzero_on_cross_service_model_with_valid_claude_list(
    tmp_path, monkeypatch
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="claude", model="gpt-5.4", effort="low"),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "plan" in result.output
    assert "gpt-5.4" in result.output
    assert "valid: ['haiku', 'opus', 'sonnet']" in result.output
    assert not build_called


def test_run_cmd_rejects_provider_prefixed_model_for_opencode_stage(
    tmp_path, monkeypatch
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(
            service="opencode",
            model="anthropic/claude-sonnet-4-5",
            effort="medium",
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "plan" in result.output
    assert "anthropic/claude-sonnet-4-5" in result.output
    assert "service='opencode'" in result.output
    assert "model='anthropic/claude-sonnet-4-5' is invalid" in result.output
    assert not build_called


def test_run_cmd_rejects_non_neutral_effort_for_opencode_stage(tmp_path, monkeypatch):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(
            service="opencode",
            model="kimi-k2.6",
            effort="low",
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "plan" in result.output
    assert "service='opencode'" in result.output
    assert "effort='low' is invalid" in result.output
    assert "valid: ['medium']" in result.output
    assert not build_called


def test_run_cmd_rejects_fallback_empty_service(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        implement_override=StageOverride(
            service="claude",
            effort="medium",
            fallback=StageOverride(service="", effort="low"),
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "stage='implement fallback': service is required" in result.output
    assert not build_called


def test_run_cmd_rejects_fallback_invalid_model(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        implement_override=StageOverride(
            service="claude",
            effort="medium",
            fallback=StageOverride(service="claude", model="gpt-5.4", effort="low"),
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert (
        "  stage='implement fallback': model='gpt-5.4' is invalid"
        " for service='claude'. (valid: ['haiku', 'opus', 'sonnet'])" in result.output
    )
    assert not build_called


def test_run_cmd_reuses_fallback_stage_label_for_each_invalid_fallback_node(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        implement_override=StageOverride(
            service="claude",
            effort="medium",
            fallback=StageOverride(
                service="claude",
                model="gpt-5.4",
                effort="low",
                fallback=StageOverride(service="", effort=""),
            ),
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert (
        "  stage='implement fallback': model='gpt-5.4' is invalid"
        " for service='claude'. (valid: ['haiku', 'opus', 'sonnet'])" in result.output
    )
    assert "  stage='implement fallback': service is required" in result.output
    assert "  stage='implement fallback': effort is required" in result.output
    assert not build_called


def test_run_cmd_reports_all_violations_in_single_message(tmp_path, monkeypatch):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
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
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "codez" in result.output
    assert "max" in result.output
    assert not build_called


def test_run_cmd_reports_missing_fields_and_invalid_models_together(
    tmp_path, monkeypatch
):
    from pycastle.config.types import StageOverride
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="", effort=""),
        implement_override=StageOverride(
            service="claude", model="gpt-5.4", effort="medium"
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert result.output.count("Config validation errors:") == 1
    assert "stage='plan': service is required" in result.output
    assert "stage='plan': effort is required" in result.output
    assert "stage='implement': model='gpt-5.4' is invalid" in result.output
    assert not build_called


def test_run_cmd_exits_nonzero_on_provider_model_mismatch_before_docker_build(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()

    class _AcceptingAdapter:
        def __init__(self, models: frozenset[str]) -> None:
            self._models = models

        def valid_models(self) -> frozenset[str]:
            return self._models

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    class _ConfiguredAdapter:
        def valid_models(self) -> frozenset[str]:
            return frozenset({"gpt-5.4"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
            fallback=StageOverride(
                service="codex",
                model="gpt-5.4-mini",
                effort="medium",
            ),
        ),
        implement_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        review_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        merge_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        preflight_issue_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        improve_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
    )
    build_called = []
    fake_svc = MagicMock()
    fake_svc.build.side_effect = lambda *a, **kw: build_called.append(True)

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch(
            "pycastle.run_startup_preparation.configured_provider_adapters_for_run",
            return_value={
                "claude": _AcceptingAdapter(frozenset({"sonnet"})),
                "codex": _ConfiguredAdapter(),
            },
        ),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "stage='plan fallback'" in result.output
    assert "model='gpt-5.4-mini' is invalid" in result.output
    assert 'Did you mean "gpt-5.4"?' in result.output
    assert not build_called


def test_run_cmd_valid_config_passes_validation_silently(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", raising=False)

    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build.return_value = None

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.main.agent_runtime.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output
    assert "Config validation" not in result.output


# ── Issue 1955: abort with init instruction when pycastle/ dir is absent ──────


def test_run_cmd_aborts_with_init_instruction_when_pycastle_dir_absent(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 1
    assert "pycastle init" in result.output


def test_run_cmd_aborts_mentions_missing_pycastle_dir_when_pycastle_dir_absent(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    result = CliRunner().invoke(cli, ["run"])

    assert "pycastle/" in result.output


def test_config_loading_commands_succeed_when_pycastle_dir_exists(
    tmp_path, monkeypatch
):
    from pycastle.main import main as cli

    (tmp_path / "pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    cfg = Config(docker_image_name="img")
    fake_svc = MagicMock()
    fake_svc.build.return_value = None

    async def _fake_run(*args, **kwargs):
        pass

    with (
        patch("pycastle.main.load_config", return_value=cfg),
        patch("pycastle.commands.build.DockerService", return_value=fake_svc),
        patch("pycastle.main.agent_runtime.run", _fake_run),
    ):
        result = CliRunner().invoke(cli, ["run"])

    assert result.exit_code == 0, result.output


def test_init_cmd_succeeds_without_pycastle_dir(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYCASTLE_HOME", str(tmp_path / "no_global"))

    with patch("pycastle.commands.init.main") as fake_init:
        result = CliRunner().invoke(cli, ["init", "--local"])

    assert result.exit_code == 0, result.output
    assert fake_init.called
