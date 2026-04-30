from unittest.mock import patch

from pycastle.config import Config


# ── Issue 203: cfg injection into _load_env ───────────────────────────────────


def test_load_env_calls_load_dotenv_with_cfg_env_file(tmp_path):
    """_load_env(cfg=Config(env_file=...)) must call load_dotenv with that path."""
    from pycastle.main import _load_env

    custom_env = tmp_path / "custom.env"

    with patch("pycastle.main.load_dotenv") as mock_dotenv:
        _load_env(cfg=Config(env_file=custom_env))

    mock_dotenv.assert_called_once_with(custom_env)
