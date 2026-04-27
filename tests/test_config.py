from pathlib import Path

import pycastle.defaults.config as defaults_config


def test_stage_overrides_exists_in_defaults():
    assert hasattr(defaults_config, "STAGE_OVERRIDES")


def test_stage_overrides_has_four_stages():
    assert set(defaults_config.STAGE_OVERRIDES.keys()) == {
        "plan",
        "implement",
        "review",
        "merge",
    }


def test_stage_overrides_each_stage_has_model_and_effort():
    for stage in ("plan", "implement", "review", "merge"):
        entry = defaults_config.STAGE_OVERRIDES[stage]
        assert "model" in entry
        assert "effort" in entry


def test_stage_overrides_defaults_are_empty_strings():
    for stage in ("plan", "implement", "review", "merge"):
        entry = defaults_config.STAGE_OVERRIDES[stage]
        assert entry["model"] == ""
        assert entry["effort"] == ""


_USER_CONFIG = Path(__file__).parent.parent / "pycastle" / "config.py"


def test_user_config_has_stage_overrides():
    user_config = _USER_CONFIG.read_text()
    assert "STAGE_OVERRIDES" in user_config


def test_user_config_has_all_four_stages():
    user_config = _USER_CONFIG.read_text()
    for stage in ("plan", "implement", "review", "merge"):
        assert f'"{stage}"' in user_config or f"'{stage}'" in user_config


def test_user_config_comment_lists_model_shorthands():
    user_config = _USER_CONFIG.read_text()
    assert "haiku" in user_config
    assert "sonnet" in user_config
    assert "opus" in user_config


def test_user_config_comment_lists_effort_values():
    user_config = _USER_CONFIG.read_text()
    assert "low" in user_config
    assert "normal" in user_config
    assert "high" in user_config
