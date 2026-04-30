import dataclasses
from unittest.mock import MagicMock

import pytest

from pycastle.claude_service import ClaudeService
from pycastle.config import Config, StageOverride, load_config
from pycastle.errors import (
    ClaudeCliNotFoundError,
    ClaudeCommandError,
    ClaudeServiceError,
    ClaudeTimeoutError,
    ConfigValidationError,
    PycastleError,
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


def test_load_config_returns_defaults_when_no_local_file(tmp_path):
    cfg = load_config(repo_root=tmp_path)
    assert cfg.max_parallel == 1
    assert cfg.issue_label == "ready-for-agent"


def test_config_has_bug_label_default():
    cfg = Config()
    assert cfg.bug_label == "bug"


def test_load_config_applies_bug_label_from_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text('bug_label = "my-bug"\n')
    cfg = load_config(repo_root=tmp_path)
    assert cfg.bug_label == "my-bug"


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
    cfg = load_config(repo_root=tmp_path, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-haiku-4-5-20251001"
    assert cfg.plan_override.effort == "low"


def test_load_config_with_empty_overrides_dict(tmp_path):
    cfg = load_config(repo_root=tmp_path, overrides={})
    assert cfg.max_parallel == 1


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
    cfg = load_config(repo_root=tmp_path, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-opus-4-7"
    assert cfg.plan_override.effort == "max"


def test_stage_override_importable_from_package_top_level():
    from pycastle import StageOverride as TopLevelStageOverride
    from pycastle.config import StageOverride as ConfigStageOverride

    assert TopLevelStageOverride is ConfigStageOverride


# ── Issue 269: UPPERCASE backward-compat aliases removed ─────────────────────


def test_config_module_does_not_export_uppercase_aliases():
    import pycastle.config as cfg_mod

    _removed = [
        "MAX_ITERATIONS",
        "MAX_PARALLEL",
        "WORKTREE_TIMEOUT",
        "IDLE_TIMEOUT",
        "DOCKER_IMAGE_NAME",
        "ISSUE_LABEL",
        "HITL_LABEL",
        "PYCASTLE_DIR",
        "PROMPTS_DIR",
        "LOGS_DIR",
        "WORKTREES_DIR",
        "ENV_FILE",
        "DOCKERFILE",
        "PREFLIGHT_CHECKS",
        "IMPLEMENT_CHECKS",
        "USAGE_LIMIT_PATTERNS",
        "STAGE_OVERRIDES",
    ]
    for name in _removed:
        assert not hasattr(cfg_mod, name), f"config.py should not export {name!r}"


# ── load_config: model shorthand resolution ───────────────────


def test_load_config_resolves_model_shorthand_in_overrides(tmp_path):
    cfg = load_config(
        repo_root=tmp_path,
        overrides={"plan_override": StageOverride(model="sonnet", effort="")},
        claude_service=_make_service(),
    )
    assert cfg.plan_override.model == "claude-sonnet-4-6"


def test_load_config_resolves_model_shorthand(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-sonnet-4-6"


def test_load_config_resolves_all_four_stage_overrides(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
        'implement_override = StageOverride(model="sonnet", effort="")\n'
        'review_override = StageOverride(model="opus", effort="")\n'
        'merge_override = StageOverride(model="haiku", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-haiku-4-5-20251001"
    assert cfg.implement_override.model == "claude-sonnet-4-6"
    assert cfg.review_override.model == "claude-opus-4-7"
    assert cfg.merge_override.model == "claude-haiku-4-5-20251001"


# ── load_config: invalid model raises ConfigValidationError ───


def test_load_config_validate_invalid_model_raises(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="gpt4", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert exc_info.value.invalid_value == "gpt4"


def test_load_config_validate_invalid_model_error_has_suggestion(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnit", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert exc_info.value.suggestion == "sonnet"


def test_load_config_validate_invalid_model_error_lists_valid_options(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="unknown", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert set(exc_info.value.valid_options) == {"haiku", "sonnet", "opus"}


# ── load_config: invalid effort raises ConfigValidationError ───


def test_load_config_validate_invalid_effort_raises(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="", effort="ultra")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert exc_info.value.invalid_value == "ultra"


def test_load_config_validate_invalid_effort_has_suggestion(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="", effort="hih")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert exc_info.value.suggestion == "high"


def test_load_config_validate_valid_efforts_pass(tmp_path):
    (tmp_path / "pycastle").mkdir()
    config_dir = tmp_path / "pycastle"
    for effort in ("low", "medium", "high", "xhigh", "max"):
        config_dir.joinpath("config.py").write_text(
            "from pycastle import StageOverride\n"
            f'plan_override = StageOverride(model="", effort="{effort}")\n'
        )
        cfg = load_config(repo_root=tmp_path, claude_service=_make_service())
        assert cfg.plan_override.effort == effort


# ── load_config: ClaudeService errors → ConfigValidationError ─


def test_load_config_validate_cli_not_found_raises_config_validation_error(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    svc = MagicMock(spec=ClaudeService)
    svc.list_models.side_effect = ClaudeCliNotFoundError("claude CLI not found")
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=svc)
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
        load_config(repo_root=tmp_path, claude_service=svc)
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
        load_config(repo_root=tmp_path, claude_service=svc)


# ── load_config: lru_cache on _fetch_models ───────────────────


def test_load_config_validate_model_list_fetched_only_once(tmp_path):
    (tmp_path / "pycastle").mkdir()
    config_dir = tmp_path / "pycastle"
    svc = _make_service()
    config_dir.joinpath("config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    load_config(repo_root=tmp_path, claude_service=svc)
    config_dir.joinpath("config.py").write_text(
        "from pycastle import StageOverride\n"
        'implement_override = StageOverride(model="haiku", effort="")\n'
    )
    load_config(repo_root=tmp_path, claude_service=svc)
    svc.list_models.assert_called_once()


# ── load_config: empty model/effort bypass validation ──────────


def test_load_config_validate_empty_model_skips_resolution(tmp_path):
    svc = _make_service()
    cfg = load_config(repo_root=tmp_path, claude_service=svc)
    assert cfg.plan_override.model == ""
    svc.list_models.assert_not_called()


# ── load_config: idempotency ──────────────────────────────────


def test_load_config_validate_already_resolved_full_id_accepted(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="claude-sonnet-4-6", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, claude_service=_make_service())
    assert cfg.plan_override.model == "claude-sonnet-4-6"


# ── load_config: atomicity ────────────────────────────────────


def test_load_config_validate_valid_model_with_invalid_effort_raises_effort_error(
    tmp_path,
):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="badeffort")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert exc_info.value.invalid_value == "badeffort"


def test_load_config_validate_atomicity_no_partial_resolution(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
        'implement_override = StageOverride(model="badmodel", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert exc_info.value.invalid_value == "badmodel"


# ── ConfigValidationError hierarchy ─────────────────────────────────────────


def test_config_validation_error_is_pycastle_error():
    assert issubclass(ConfigValidationError, PycastleError)


def test_config_validation_error_carries_fields():
    err = ConfigValidationError(
        "bad value",
        invalid_value="foo",
        suggestion="bar",
        valid_options=["bar", "baz"],
    )
    assert err.invalid_value == "foo"
    assert err.suggestion == "bar"
    assert err.valid_options == ["bar", "baz"]


def test_config_validation_error_defaults_are_empty():
    err = ConfigValidationError("msg")
    assert err.invalid_value == ""
    assert err.suggestion == ""
    assert err.valid_options == []


# ── load_config: semver resolution ────────────────────────────


def test_load_config_validate_highest_semver_wins_for_shorthand(tmp_path):
    models = ("claude-sonnet-3-5", "claude-sonnet-4-6", "claude-sonnet-4-5-20241022")
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, claude_service=_make_service(models))
    assert cfg.plan_override.model == "claude-sonnet-4-6"


def test_load_config_validate_newest_patch_wins_over_older_minor(tmp_path):
    models = ("claude-haiku-3-5", "claude-haiku-4-5-20251001")
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path, claude_service=_make_service(models))
    assert cfg.plan_override.model == "claude-haiku-4-5-20251001"


# ── load_config: additional error cases ───────────────────────


def test_load_config_validate_invalid_effort_lists_valid_options(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="", effort="ultra")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert set(exc_info.value.valid_options) == {
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }


def test_load_config_validate_command_error_raises_config_validation_error(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    svc = MagicMock(spec=ClaudeService)
    svc.list_models.side_effect = ClaudeCommandError("exit 127")
    with pytest.raises(ConfigValidationError):
        load_config(repo_root=tmp_path, claude_service=svc)


def test_load_config_validate_no_parseable_models_empty_valid_options(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(
            repo_root=tmp_path,
            claude_service=_make_service(("gpt-4", "gpt-3.5-turbo")),
        )
    assert exc_info.value.invalid_value == "sonnet"
    assert exc_info.value.valid_options == []


def test_load_config_validate_full_looking_unknown_id_raises(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="claude-haiku-99-0", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert exc_info.value.invalid_value == "claude-haiku-99-0"


def test_load_config_validate_service_error_not_cached_retry_succeeds(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    svc = MagicMock(spec=ClaudeService)
    svc.list_models.side_effect = ClaudeServiceError("transient")
    with pytest.raises(ConfigValidationError):
        load_config(repo_root=tmp_path, claude_service=svc)

    svc.list_models.side_effect = None
    svc.list_models.return_value = _FAKE_MODELS
    cfg = load_config(repo_root=tmp_path, claude_service=svc)
    assert cfg.plan_override.model == "claude-sonnet-4-6"
    assert svc.list_models.call_count == 2


def test_load_config_validate_two_services_fetch_independently(tmp_path):
    (tmp_path / "pycastle").mkdir()
    svc1 = _make_service(("claude-sonnet-4-6",))
    svc2 = _make_service(("claude-haiku-4-5-20251001",))
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    cfg1 = load_config(repo_root=tmp_path, claude_service=svc1)
    assert cfg1.plan_override.model == "claude-sonnet-4-6"
    svc1.list_models.assert_called_once()

    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
    )
    cfg2 = load_config(repo_root=tmp_path, claude_service=svc2)
    assert cfg2.plan_override.model == "claude-haiku-4-5-20251001"
    svc2.list_models.assert_called_once()


def test_load_config_validate_multiple_empty_models_no_claude_call(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="", effort="low")\n'
        'implement_override = StageOverride(model="", effort="")\n'
    )
    svc = _make_service()
    load_config(repo_root=tmp_path, claude_service=svc)
    svc.list_models.assert_not_called()


# ── validate_config as a standalone public function ───────────────────────────


def test_validate_config_is_importable_from_config_package():
    from pycastle.config import validate_config

    assert callable(validate_config)


def test_validate_config_resolves_model_shorthand_directly():
    from pycastle.config import validate_config

    cfg = Config(plan_override=StageOverride(model="sonnet", effort=""))
    result = validate_config(cfg, _make_service())
    assert result.plan_override.model == "claude-sonnet-4-6"


def test_validate_config_returns_new_config_does_not_mutate():
    from pycastle.config import validate_config

    cfg = Config(plan_override=StageOverride(model="haiku", effort=""))
    result = validate_config(cfg, _make_service())
    assert result is not cfg
    assert cfg.plan_override.model == "haiku"
    assert result.plan_override.model == "claude-haiku-4-5-20251001"


def test_validate_config_raises_for_invalid_effort_directly():
    from pycastle.config import validate_config

    cfg = Config(plan_override=StageOverride(model="", effort="badvalue"))
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(cfg, _make_service())
    assert exc_info.value.invalid_value == "badvalue"


# ── edge cases ────────────────────────────────────────────────────────────────


def test_load_config_validate_empty_models_tuple_raises_for_any_shorthand(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(
            repo_root=tmp_path,
            claude_service=_make_service(()),
        )
    assert exc_info.value.invalid_value == "sonnet"
    assert exc_info.value.valid_options == []


def test_load_config_validate_non_claude_models_only_raises_for_shorthand(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(
            repo_root=tmp_path,
            claude_service=_make_service(("gpt-4o", "gemini-pro")),
        )
    assert exc_info.value.valid_options == []


def test_load_config_validate_effort_error_names_the_stage(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'implement_override = StageOverride(model="", effort="turbo")\n'
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, claude_service=_make_service())
    assert "implement" in str(exc_info.value)
