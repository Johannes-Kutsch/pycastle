from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from pycastle.config import Config
from pycastle.errors import ClaudeCliNotFoundError, ConfigValidationError


# ── Issue 203: cfg injection into _load_env ───────────────────────────────────


def test_load_env_calls_load_dotenv_with_cfg_env_file(tmp_path):
    """_load_env(cfg=Config(env_file=...)) must call load_dotenv with that path."""
    from pycastle.main import _load_env

    custom_env = tmp_path / "custom.env"

    with patch("pycastle.main.load_dotenv") as mock_dotenv:
        _load_env(cfg=Config(env_file=custom_env))

    mock_dotenv.assert_called_once_with(custom_env)


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

    def fake_gh(method, path, token, data=None):
        if method == "POST":
            posted.append(data)
        return (201, None)

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr("pycastle.labels.click.prompt", lambda *a, **kw: "owner/repo")
    monkeypatch.setattr("pycastle.labels.click.confirm", lambda *a, **kw: False)

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.labels._gh", fake_gh):
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
