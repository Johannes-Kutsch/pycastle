import dataclasses
from pathlib import Path

import pytest

from pycastle.config import Config, StageOverride, load_config
from pycastle.config.loader import describe_config_layers, resolve_global_dir
from pycastle.errors import (
    ConfigValidationError,
    PycastleError,
)


def test_load_config_returns_defaults_when_no_local_file(tmp_path):
    cfg = load_config(repo_root=tmp_path)
    assert cfg.max_parallel == 1
    assert cfg.issue_label == "ready-for-agent"


def test_config_has_bug_label_default():
    cfg = Config()
    assert cfg.bug_label == "bug"


def test_config_has_enhancement_label_default():
    assert Config().enhancement_label == "enhancement"


def test_config_has_needs_triage_label_default():
    assert Config().needs_triage_label == "needs-triage"


def test_config_has_needs_info_label_default():
    assert Config().needs_info_label == "needs-info"


def test_config_has_wontfix_label_default():
    assert Config().wontfix_label == "wontfix"


def test_config_has_auto_file_bugs_default_false():
    assert Config().auto_file_bugs is False


def test_config_has_bug_report_repo_default():
    assert Config().bug_report_repo == "Johannes-Kutsch/pycastle"


@pytest.mark.parametrize("bad", ["justonename", "a/b/c", "", "/x", "x/"])
def test_load_config_rejects_malformed_bug_report_repo(tmp_path, bad):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(f"bug_report_repo = {bad!r}\n")
    with pytest.raises(ConfigValidationError) as ei:
        load_config(repo_root=tmp_path)
    assert ei.value.suggestion == "Johannes-Kutsch/pycastle"
    assert ei.value.invalid_value == bad


def test_load_config_accepts_valid_bug_report_repo(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        'bug_report_repo = "owner/other-repo"\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.bug_report_repo == "owner/other-repo"


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


def test_load_config_silently_ignores_usage_limit_patterns_in_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        'usage_limit_patterns = ("foo",)\nmax_parallel = 3\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.max_parallel == 3
    assert not hasattr(cfg, "usage_limit_patterns")


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


# ── Issue 222: load_config without repo_root uses CWD ────────────────────────


def test_load_config_without_repo_root_uses_cwd(tmp_path, monkeypatch):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text('docker_image_name = "myapp"\n')
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.docker_image_name == "myapp"


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


# ── load_config: model string passthrough ─────────────────────────────────────


def test_load_config_model_string_passes_through_unchanged(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="sonnet", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override.model == "sonnet"


def test_load_config_full_model_id_passes_through_unchanged(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="claude-sonnet-4-6", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override.model == "claude-sonnet-4-6"


def test_load_config_all_stage_model_strings_pass_through(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'plan_override = StageOverride(model="haiku", effort="")\n'
        'implement_override = StageOverride(model="sonnet", effort="")\n'
        'review_override = StageOverride(model="opus", effort="")\n'
        'merge_override = StageOverride(model="haiku", effort="")\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.plan_override.model == "haiku"
    assert cfg.implement_override.model == "sonnet"
    assert cfg.review_override.model == "opus"
    assert cfg.merge_override.model == "haiku"


def test_load_config_validate_valid_efforts_pass(tmp_path):
    (tmp_path / "pycastle").mkdir()
    config_dir = tmp_path / "pycastle"
    for effort in ("low", "medium", "high", "xhigh", "max"):
        config_dir.joinpath("config.py").write_text(
            "from pycastle import StageOverride\n"
            f'plan_override = StageOverride(model="", effort="{effort}")\n'
        )
        cfg = load_config(repo_root=tmp_path)
        assert cfg.plan_override.effort == effort


def test_load_config_accepts_codex_efforts_at_load_time(tmp_path):
    (tmp_path / "pycastle").mkdir()
    config_dir = tmp_path / "pycastle"
    for effort in ("none", "minimal"):
        config_dir.joinpath("config.py").write_text(
            "from pycastle import StageOverride\n"
            f'plan_override = StageOverride(model="", effort="{effort}")\n'
        )
        cfg = load_config(repo_root=tmp_path)
        assert cfg.plan_override.effort == effort


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


# ── Issue 418: auto_push config field ────────────────────────────────────────


def test_config_auto_push_defaults_to_true():
    cfg = Config()
    assert cfg.auto_push is True


def test_load_config_applies_auto_push_false_from_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("auto_push = False\n")
    cfg = load_config(repo_root=tmp_path)
    assert cfg.auto_push is False


# ── Issue 479: preflight_issue_override stage override ─────────────────────


def test_config_has_preflight_issue_override_field_with_empty_default():
    cfg = Config()
    assert cfg.preflight_issue_override == StageOverride()


def test_load_config_applies_preflight_issue_override_from_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'preflight_issue_override = StageOverride(model="opus", effort="high")\n'
    )
    cfg = load_config(repo_root=tmp_path)
    assert cfg.preflight_issue_override.model == "opus"
    assert cfg.preflight_issue_override.effort == "high"


# ── Issue 472: global config.py layer + path-field guard ───────────────────


def test_resolve_global_dir_prefers_explicit_arg(tmp_path):
    explicit = tmp_path / "explicit"
    resolved = resolve_global_dir(explicit, {"PYCASTLE_HOME": "/from/env"})
    assert resolved == explicit


def test_resolve_global_dir_falls_back_to_env_var():
    resolved = resolve_global_dir(None, {"PYCASTLE_HOME": "/from/env"})
    assert resolved == Path("/from/env")


def test_resolve_global_dir_falls_back_to_platformdirs():
    import platformdirs

    resolved = resolve_global_dir(None, {})
    assert resolved == Path(platformdirs.user_config_dir("pycastle"))


def test_load_config_no_global_returns_defaults(tmp_path):
    cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    assert cfg.max_parallel == 1


def test_load_config_global_only_applies(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text("max_parallel = 7\n")
    cfg = load_config(repo_root=tmp_path, global_dir=global_dir)
    assert cfg.max_parallel == 7


def test_load_config_local_overrides_global(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "max_parallel = 7\nbug_label = 'global-bug'\n"
    )
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("max_parallel = 4\n")
    cfg = load_config(repo_root=tmp_path, global_dir=global_dir)
    # local overrides global for max_parallel
    assert cfg.max_parallel == 4
    # global value preserved when not set locally
    assert cfg.bug_label == "global-bug"


def test_load_config_global_path_field_pycastle_dir_raises(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "from pathlib import Path\npycastle_dir = Path('elsewhere')\n"
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, global_dir=global_dir)
    assert "pycastle_dir" in str(exc_info.value)


def test_load_config_global_path_field_lists_all_offending(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "from pathlib import Path\nprompts_dir = Path('p')\nlogs_dir = Path('l')\n"
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, global_dir=global_dir)
    msg = str(exc_info.value)
    assert "prompts_dir" in msg
    assert "logs_dir" in msg


def test_load_config_local_path_fields_still_allowed(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pathlib import Path\nprompts_dir = Path('custom-prompts')\n"
    )
    cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    assert cfg.prompts_dir == Path("custom-prompts")


def test_load_config_pycastle_home_env_resolves_global_dir(tmp_path, monkeypatch):
    global_dir = tmp_path / "from_env"
    global_dir.mkdir()
    (global_dir / "config.py").write_text("max_parallel = 5\n")
    (tmp_path / "pycastle").mkdir()  # repo_root has no pycastle/config.py
    monkeypatch.setenv("PYCASTLE_HOME", str(global_dir))
    cfg = load_config(repo_root=tmp_path)
    assert cfg.max_parallel == 5


def test_load_config_explicit_global_dir_overrides_env(tmp_path, monkeypatch):
    env_dir = tmp_path / "env_dir"
    env_dir.mkdir()
    (env_dir / "config.py").write_text("max_parallel = 99\n")
    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    (explicit_dir / "config.py").write_text("max_parallel = 42\n")
    monkeypatch.setenv("PYCASTLE_HOME", str(env_dir))
    cfg = load_config(repo_root=tmp_path, global_dir=explicit_dir)
    assert cfg.max_parallel == 42


def test_load_config_nonexistent_global_dir_is_hermetic(tmp_path):
    cfg = load_config(repo_root=tmp_path, global_dir=Path("/nonexistent"))
    assert cfg.max_parallel == 1


def test_load_config_global_unknown_key_still_raises(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text("not_a_real_key = 1\n")
    with pytest.raises(ValueError, match="not_a_real_key"):
        load_config(repo_root=tmp_path, global_dir=global_dir)


def test_load_config_overrides_take_precedence_over_global(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text("max_parallel = 7\n")
    cfg = load_config(
        repo_root=tmp_path,
        global_dir=global_dir,
        overrides={"max_parallel": 99},
    )
    assert cfg.max_parallel == 99


# ── Issue 484: docker_image_name is per-project ──────────────────────────────


def test_load_config_global_docker_image_name_raises(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text('docker_image_name = "shared"\n')
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config(repo_root=tmp_path, global_dir=global_dir)
    assert "docker_image_name" in str(exc_info.value)


def test_load_config_unset_docker_image_name_derives_from_cwd(tmp_path, monkeypatch):
    project_dir = tmp_path / "My Project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    cfg = load_config(repo_root=project_dir, global_dir=tmp_path / "no_global")
    assert cfg.docker_image_name == "my-project"


def test_load_config_local_docker_image_name_overrides_derived(tmp_path, monkeypatch):
    project_dir = tmp_path / "Some Project"
    project_dir.mkdir()
    (project_dir / "pycastle").mkdir()
    (project_dir / "pycastle" / "config.py").write_text(
        'docker_image_name = "explicit-name"\n'
    )
    monkeypatch.chdir(project_dir)
    cfg = load_config(repo_root=project_dir, global_dir=tmp_path / "no_global")
    assert cfg.docker_image_name == "explicit-name"


# ── Issue 475: layer summary line ─────────────────────────────────────────


def test_describe_config_layers_defaults_only_returns_defaults_label(tmp_path):
    summary = describe_config_layers(
        repo_root=tmp_path, global_dir=tmp_path / "no_global"
    )

    assert summary == "Config: defaults"


def test_describe_config_layers_with_local_only_appends_pycastle_path(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("")

    summary = describe_config_layers(
        repo_root=tmp_path, global_dir=tmp_path / "no_global"
    )

    assert summary == "Config: defaults + pycastle/config.py"


def test_describe_config_layers_with_global_only_appends_global_path(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text("")

    summary = describe_config_layers(repo_root=tmp_path, global_dir=global_dir)

    expected_path = (global_dir / "config.py").as_posix()
    assert summary == f"Config: defaults + {expected_path}"
    assert "pycastle/config.py" not in summary


def test_describe_config_layers_with_both_layers_orders_global_then_local(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text("")
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text("")

    summary = describe_config_layers(repo_root=tmp_path, global_dir=global_dir)

    expected_global = (global_dir / "config.py").as_posix()
    assert summary == (f"Config: defaults + {expected_global} + pycastle/config.py")


def test_describe_config_layers_shortens_home_path_to_tilde(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    global_dir = fake_home / ".config" / "pycastle"
    global_dir.mkdir(parents=True)
    (global_dir / "config.py").write_text("")
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    summary = describe_config_layers(repo_root=tmp_path, global_dir=global_dir)

    assert summary == "Config: defaults + ~/.config/pycastle/config.py"


def test_describe_config_layers_uses_appdata_form_on_windows(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    global_dir = appdata / "pycastle"
    global_dir.mkdir(parents=True)
    (global_dir / "config.py").write_text("")
    monkeypatch.setattr("pycastle.config.loader.os.name", "nt")
    monkeypatch.setenv("APPDATA", str(appdata))

    summary = describe_config_layers(repo_root=tmp_path, global_dir=global_dir)

    assert summary == r"Config: defaults + %APPDATA%\pycastle\config.py"


# ── Issue 613: improve_override defaults to opus/high ─────────────────────


def test_config_improve_override_default_is_opus_high():
    cfg = Config()
    assert cfg.improve_override.model == "opus"
    assert cfg.improve_override.effort == "high"


def test_load_config_improve_override_default_is_opus_high(tmp_path):
    cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    assert cfg.improve_override.model == "opus"
    assert cfg.improve_override.effort == "high"


def test_load_config_project_improve_override_takes_precedence(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        'improve_override = StageOverride(model="sonnet", effort="medium")\n'
    )
    cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    assert cfg.improve_override.model == "sonnet"
    assert cfg.improve_override.effort == "medium"


# ── Issue 655: improve_max field ────────────────────────────────────────────


def test_config_improve_max_defaults_to_none():
    assert Config().improve_max is None


@pytest.mark.parametrize("value", [None, 1, 5, 1000])
def test_load_config_improve_max_accepts_none_and_positive(tmp_path, value):
    if value is None:
        cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    else:
        cfg = load_config(
            repo_root=tmp_path,
            global_dir=tmp_path / "no_global",
            overrides={"improve_max": value},
        )
    assert cfg.improve_max == value


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_load_config_improve_max_rejects_zero_and_negative(tmp_path, bad):
    with pytest.raises(ConfigValidationError, match="improve_max must be >= 1"):
        load_config(
            repo_root=tmp_path,
            global_dir=tmp_path / "no_global",
            overrides={"improve_max": bad},
        )


# ── Issue 670: improve_mode field ───────────────────────────────────────────


def test_config_improve_mode_defaults_to_none():
    assert Config().improve_mode is None


@pytest.mark.parametrize("value", [None, "until_sleep", "endless"])
def test_load_config_improve_mode_accepts_none_and_valid(tmp_path, value):
    if value is None:
        cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    else:
        cfg = load_config(
            repo_root=tmp_path,
            global_dir=tmp_path / "no_global",
            overrides={"improve_mode": value},
        )
    assert cfg.improve_mode == value


@pytest.mark.parametrize("bad", ["UNTIL_SLEEP", "forever", "", "true"])
def test_load_config_improve_mode_rejects_invalid(tmp_path, bad):
    with pytest.raises(ConfigValidationError, match="improve_mode"):
        load_config(
            repo_root=tmp_path,
            global_dir=tmp_path / "no_global",
            overrides={"improve_mode": bad},
        )


# ── Issue 783: per-stage service + fallback, top-level default_service ──────


def test_config_default_service_defaults_to_claude():
    assert Config().default_service == "claude"


def test_stage_override_service_defaults_to_empty_string():
    assert StageOverride().service == ""


def test_stage_override_fallback_defaults_to_none():
    assert StageOverride().fallback is None


def test_load_config_round_trips_default_service_from_local_file(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text('default_service = "codex"\n')
    cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    assert cfg.default_service == "codex"


def test_load_config_round_trips_default_service_from_global_file(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text('default_service = "codex"\n')
    cfg = load_config(repo_root=tmp_path, global_dir=global_dir)
    assert cfg.default_service == "codex"


def test_load_config_round_trips_stage_override_service_and_fallback(tmp_path):
    (tmp_path / "pycastle").mkdir()
    (tmp_path / "pycastle" / "config.py").write_text(
        "from pycastle import StageOverride\n"
        "plan_override = StageOverride(\n"
        '    service="claude",\n'
        '    model="opus",\n'
        '    effort="high",\n'
        '    fallback=StageOverride(service="codex", model="gpt-5", effort="medium"),\n'
        ")\n"
    )
    cfg = load_config(repo_root=tmp_path, global_dir=tmp_path / "no_global")
    assert cfg.plan_override.service == "claude"
    assert cfg.plan_override.fallback == StageOverride(
        service="codex", model="gpt-5", effort="medium"
    )
