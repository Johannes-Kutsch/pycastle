
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