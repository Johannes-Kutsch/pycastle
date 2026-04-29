import dataclasses

import pytest

from pycastle.config import Config, StageOverride, load_config


def test_load_config_returns_defaults_when_no_local_file(tmp_path):
    cfg = load_config(repo_root=tmp_path)
    assert cfg.max_parallel == 1
    assert cfg.issue_label == "ready-for-agent"


def test_load_config_applies_local_file_override(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("max_parallel = 4\n")
    cfg = load_config(repo_root=tmp_path)
    assert cfg.max_parallel == 4


def test_load_config_raises_for_unknown_key_in_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("max_paralell = 4\n")
    with pytest.raises(ValueError, match="max_paralell"):
        load_config(repo_root=tmp_path)


def test_load_config_applies_in_process_overrides(tmp_path):
    cfg = load_config(repo_root=tmp_path, overrides={"max_parallel": 4})
    assert cfg.max_parallel == 4


def test_load_config_raises_for_unknown_override_key(tmp_path):
    with pytest.raises(ValueError, match="no_such_key"):
        load_config(repo_root=tmp_path, overrides={"no_such_key": 99})


def test_config_and_stage_override_are_constructable_inline():
    override = StageOverride(model="haiku", effort="low")
    cfg = Config(max_parallel=4, plan_override=override)
    assert cfg.max_parallel == 4
    assert cfg.plan_override.model == "haiku"


def test_dataclasses_replace_works_on_config():
    cfg = Config()
    updated = dataclasses.replace(cfg, max_parallel=8)
    assert updated.max_parallel == 8
    assert cfg.max_parallel == 1  # original unchanged


def test_config_is_frozen():
    cfg = Config()
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        cfg.max_parallel = 99  # type: ignore[misc]


def test_overrides_take_precedence_over_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("max_parallel = 4\n")
    cfg = load_config(repo_root=tmp_path, overrides={"max_parallel": 99})
    assert cfg.max_parallel == 99


def test_load_config_applies_stage_override_from_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="low")\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override.model == "haiku"
    assert cfg.plan_override.effort == "low"


def test_load_config_with_empty_overrides_dict(tmp_path):
    cfg = load_config(repo_root=tmp_path, overrides={})
    assert cfg.max_parallel == 1


def test_module_level_config_singleton_picks_up_local_override():
    from pycastle.config import config

    assert config.docker_image_name == "pycastle"
    assert config.max_parallel == 4


# ── Issue 222: load_config without repo_root uses CWD ────────────────────────


def test_load_config_without_repo_root_uses_cwd(tmp_path, monkeypatch):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text('docker_image_name = "myapp"\n')
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.docker_image_name == "myapp"


def test_load_config_applies_stage_override_from_local_file_using_package_import(
    tmp_path,
):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="opus", effort="max")\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override.model == "opus"
    assert cfg.plan_override.effort == "max"


def test_stage_override_importable_from_package_top_level():
    from pycastle import StageOverride as TopLevelStageOverride
    from pycastle.config import StageOverride as ConfigStageOverride

    assert TopLevelStageOverride is ConfigStageOverride
