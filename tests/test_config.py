import pycastle.defaults.config as defaults_config


def test_docker_image_name_exists_in_defaults():
    assert hasattr(defaults_config, "DOCKER_IMAGE_NAME")


def test_docker_image_legacy_name_does_not_exist():
    assert not hasattr(defaults_config, "DOCKER_IMAGE")


def test_placeholder_not_in_defaults_config():
    assert not hasattr(defaults_config, "PLACEHOLDER")


def test_shell_expr_not_in_defaults_config():
    assert not hasattr(defaults_config, "SHELL_EXPR")


def test_issue_label_matches_label_ready_for_agent():
    from pycastle.labels import LABEL_READY_FOR_AGENT

    assert defaults_config.ISSUE_LABEL == LABEL_READY_FOR_AGENT


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
