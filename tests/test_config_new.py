import dataclasses
from unittest.mock import MagicMock

import pytest

from pycastle.claude_service import ClaudeService
from pycastle.config import Config, StageOverride, load_config
from pycastle.errors import (
    ClaudeCliNotFoundError,
    ClaudeServiceError,
    ClaudeTimeoutError,
    ConfigValidationError,
)

_FAKE_MODELS = (
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
)


def _make_service(models: tuple[str, ...] = _FAKE_MODELS) -> ClaudeService:
    mock = MagicMock(spec=ClaudeService)
    mock.list_models.return_value = models
    return mock


@pytest.fixture(autouse=True)
def _clear_model_cache():
    from pycastle.config import _fetch_models

    _fetch_models.cache_clear()
    yield
    _fetch_models.cache_clear()


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


# ── load_config(validate=True): model shorthand resolution ───────────────────


def test_load_config_validate_resolves_model_shorthand(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-sonnet-4-6"


def test_load_config_validate_resolves_all_four_stage_overrides(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
        'implement_override = StageOverride(model="sonnet", effort="")\n'
        'review_override = StageOverride(model="opus", effort="")\n'
        'merge_override = StageOverride(model="haiku", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-haiku-4-5-20251001"
    assert cfg.implement_override.model == "claude-sonnet-4-6"
    assert cfg.review_override.model == "claude-opus-4-7"
    assert cfg.merge_override.model == "claude-haiku-4-5-20251001"


# ── load_config(validate=False): no subprocess calls ─────────────────────────


def test_load_config_default_does_not_call_claude(tmp_path):
    svc = _make_service()
    load_config(repo_root=tmp_path, validate=False, claude_service=svc)
    svc.list_models.assert_not_called()


def test_load_config_default_leaves_shorthands_unresolved(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override.model == "sonnet"


# ── load_config(validate=True): invalid model raises ConfigValidationError ───


def test_load_config_validate_invalid_model_raises(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="gpt4", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert exc_info.value.invalid_value == "gpt4"


def test_load_config_validate_invalid_model_error_has_suggestion(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnit", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert exc_info.value.suggestion == "sonnet"


def test_load_config_validate_invalid_model_error_lists_valid_options(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="unknown", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert set(exc_info.value.valid_options) == {"haiku", "sonnet", "opus"}


# ── load_config(validate=True): invalid effort raises ConfigValidationError ───


def test_load_config_validate_invalid_effort_raises(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="", effort="ultra")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert exc_info.value.invalid_value == "ultra"


def test_load_config_validate_invalid_effort_has_suggestion(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="", effort="hih")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert exc_info.value.suggestion == "high"


def test_load_config_validate_valid_efforts_pass(tmp_path):
    (tmp_path / "pycastle").mkdir()
    config_dir = tmp_path / "pycastle"
    for effort in ("low", "medium", "high", "xhigh", "max"):
        from pycastle.config import _fetch_models

        _fetch_models.cache_clear()
        config_dir.joinpath("config.py").write_text(
            "from pycastle import StageOverride\n"
            f'plan_override = StageOverride(model="", effort="{effort}")\n'
        )
        cfg = load_config(
            repo_root=tmp_path, validate=True, claude_service=_make_service()
        )
        assert cfg.plan_override.effort == effort


# ── load_config(validate=True): ClaudeService errors → ConfigValidationError ─


def test_load_config_validate_cli_not_found_raises_config_validation_error(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    svc = MagicMock(spec=ClaudeService)
    svc.list_models.side_effect = ClaudeCliNotFoundError("claude CLI not found")
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, validate=True, claude_service=svc)
    assert "claude" in str(exc_info.value).lower()


def test_load_config_validate_service_error_message_preserved(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    svc = MagicMock(spec=ClaudeService)
    svc.list_models.side_effect = ClaudeServiceError("very specific error text")
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, validate=True, claude_service=svc)
    assert "very specific error text" in str(exc_info.value)


def test_load_config_validate_timeout_raises_config_validation_error(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    svc = MagicMock(spec=ClaudeService)
    svc.list_models.side_effect = ClaudeTimeoutError("timed out")
    with pytest.raises(ConfigValidationError):
        load_config(repo_root=tmp_path, validate=True, claude_service=svc)


# ── load_config(validate=True): lru_cache on _fetch_models ───────────────────


def test_load_config_validate_model_list_fetched_only_once(tmp_path):
    (tmp_path / "pycastle").mkdir()
    config_dir = tmp_path / "pycastle"
    svc = _make_service()
    config_dir.joinpath("config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    load_config(repo_root=tmp_path, validate=True, claude_service=svc)
    config_dir.joinpath("config.py").write_text(
        "from pycastle import StageOverride\n"
        'implement_override = StageOverride(model="haiku", effort="")\n'
    )
    load_config(repo_root=tmp_path, validate=True, claude_service=svc)
    svc.list_models.assert_called_once()


# ── load_config(validate=True): empty model/effort bypass validation ──────────


def test_load_config_validate_empty_model_skips_resolution(tmp_path):
    svc = _make_service()
    cfg = load_config(repo_root=tmp_path, validate=True, claude_service=svc)
    assert cfg.plan_override.model == ""
    svc.list_models.assert_not_called()


# ── load_config(validate=True): idempotency ──────────────────────────────────


def test_load_config_validate_already_resolved_full_id_accepted(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="claude-sonnet-4-6", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-sonnet-4-6"


# ── load_config(validate=True): atomicity ────────────────────────────────────


def test_load_config_validate_atomicity_no_partial_resolution(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
        'implement_override = StageOverride(model="badmodel", effort="")\n'
    )
    with pytest.raises(ConfigValidationError):
        load_config(repo_root=tmp_path, validate=True, claude_service=_make_service())
