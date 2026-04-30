from unittest.mock import patch

from click.testing import CliRunner

from pycastle.config import Config
from pycastle.errors import ConfigValidationError


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


def test_build_cmd_passes_cfg_to_build_main(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config(docker_image_name="img")
    received: list[Config] = []

    def fake_build(no_cache=False, cfg=None, **kw):
        received.append(cfg)

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.build_command.main", fake_build):
            CliRunner().invoke(cli, ["build"])

    assert received == [cfg]


def test_labels_cmd_passes_cfg_to_labels_main(tmp_path, monkeypatch):
    from pycastle.main import main as cli

    monkeypatch.chdir(tmp_path)
    cfg = Config()
    received: list[Config] = []

    def fake_labels(cfg=None):
        received.append(cfg)

    with patch("pycastle.main.load_config", return_value=cfg):
        with patch("pycastle.labels.main", fake_labels):
            CliRunner().invoke(cli, ["labels"])

    assert received == [cfg]
