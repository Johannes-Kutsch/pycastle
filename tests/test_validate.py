import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pycastle.errors import ConfigValidationError, PycastleError


# ── ConfigValidationError hierarchy ──────────────────────────────────────────


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


# ── helpers ───────────────────────────────────────────────────────────────────


_FAKE_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
]


def _subprocess_ok(models: list[str] = _FAKE_MODELS):
    result = MagicMock()
    result.returncode = 0
    result.stdout = "\n".join(models) + "\n"
    result.stderr = ""
    return result


def _subprocess_fail(returncode: int = 1, stderr: str = "unknown subcommand"):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = ""
    result.stderr = stderr
    return result


@pytest.fixture(autouse=True)
def _clear_model_cache():
    from pycastle.validate import _fetch_models

    _fetch_models.cache_clear()
    yield
    _fetch_models.cache_clear()


# ── validate_config: empty overrides are a no-op ─────────────────────────────


def test_empty_overrides_do_not_call_claude():
    with patch("subprocess.run") as mock_run:
        from pycastle.validate import validate_config

        validate_config({})
        mock_run.assert_not_called()


# ── validate_config: empty model/effort strings pass without modification ─────


def test_empty_model_skips_validation_and_leaves_value():
    overrides = {"plan": {"model": "", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        validate_config(overrides)
    assert overrides["plan"]["model"] == ""


def test_empty_effort_skips_validation_and_leaves_value():
    overrides = {"plan": {"model": "", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        validate_config(overrides)
    assert overrides["plan"]["effort"] == ""


# ── validate_config: valid shorthand resolves to full model ID ────────────────


def test_valid_shorthand_resolves_to_full_model_id():
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        validate_config(overrides)
    assert overrides["plan"]["model"] == "claude-sonnet-4-6"


def test_haiku_shorthand_resolves():
    overrides = {"implement": {"model": "haiku", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        validate_config(overrides)
    assert overrides["implement"]["model"] == "claude-haiku-4-5-20251001"


def test_opus_shorthand_resolves():
    overrides = {"review": {"model": "opus", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        validate_config(overrides)
    assert overrides["review"]["model"] == "claude-opus-4-7"


# ── validate_config: highest semver wins when multiple versions exist ─────────


def test_highest_semver_wins_for_shorthand():
    models = [
        "claude-sonnet-3-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5-20241022",
    ]
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok(models)):
        from pycastle.validate import validate_config

        validate_config(overrides)
    assert overrides["plan"]["model"] == "claude-sonnet-4-6"


def test_newest_patch_wins_over_older_minor():
    models = [
        "claude-haiku-3-5",
        "claude-haiku-4-5-20251001",
    ]
    overrides = {"plan": {"model": "haiku", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok(models)):
        from pycastle.validate import validate_config

        validate_config(overrides)
    assert overrides["plan"]["model"] == "claude-haiku-4-5-20251001"


# ── validate_config: invalid model raises ConfigValidationError ───────────────


def test_invalid_model_raises_config_validation_error():
    overrides = {"plan": {"model": "gpt4", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(overrides)
    assert exc_info.value.invalid_value == "gpt4"


def test_invalid_model_error_has_suggestion():
    overrides = {"plan": {"model": "sonnit", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(overrides)
    assert exc_info.value.suggestion == "sonnet"


def test_invalid_model_error_lists_valid_options():
    overrides = {"plan": {"model": "unknown", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(overrides)
    assert set(exc_info.value.valid_options) == {"haiku", "sonnet", "opus"}


# ── validate_config: invalid effort raises ConfigValidationError ─────────────


def test_invalid_effort_raises_config_validation_error():
    overrides = {"plan": {"model": "", "effort": "ultra"}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(overrides)
    assert exc_info.value.invalid_value == "ultra"


def test_invalid_effort_error_has_suggestion():
    overrides = {"plan": {"model": "", "effort": "hih"}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(overrides)
    assert exc_info.value.suggestion == "high"


def test_invalid_effort_error_lists_valid_options():
    overrides = {"plan": {"model": "", "effort": "max"}}
    with patch("subprocess.run", return_value=_subprocess_ok()):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(overrides)
    assert set(exc_info.value.valid_options) == {"low", "normal", "high"}


def test_valid_effort_values_pass():
    for effort in ("low", "normal", "high"):
        from pycastle.validate import _fetch_models, validate_config

        _fetch_models.cache_clear()
        overrides = {"plan": {"model": "", "effort": effort}}
        with patch("subprocess.run", return_value=_subprocess_ok()):
            validate_config(overrides)
        assert overrides["plan"]["effort"] == effort


# ── validate_config: claude list-models unavailable ──────────────────────────


def test_file_not_found_raises_config_validation_error():
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with patch("subprocess.run", side_effect=FileNotFoundError):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(overrides)
    assert "claude" in str(exc_info.value).lower()


def test_nonzero_exit_raises_config_validation_error():
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_fail(returncode=127)):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError):
            validate_config(overrides)


def test_timeout_raises_config_validation_error():
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)):
        from pycastle.validate import validate_config

        with pytest.raises(ConfigValidationError):
            validate_config(overrides)


# ── validate_config: list-models is only called once (cached) ─────────────────


def test_list_models_called_once_across_multiple_validations():
    overrides1 = {"plan": {"model": "sonnet", "effort": ""}}
    overrides2 = {"implement": {"model": "haiku", "effort": ""}}
    with patch("subprocess.run", return_value=_subprocess_ok()) as mock_run:
        from pycastle.validate import validate_config

        validate_config(overrides1)
        validate_config(overrides2)
    mock_run.assert_called_once()


# ── integration test: real claude list-models ─────────────────────────────────


def _claude_list_models_available() -> bool:
    """Return True only when claude list-models exits 0 and returns parseable model IDs."""
    import re

    _model_re = re.compile(r"^claude-(haiku|sonnet|opus)-\S+$")
    try:
        result = subprocess.run(
            ["claude", "list-models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        return any(_model_re.match(line) for line in lines)
    except Exception:
        return False


@pytest.mark.skipif(
    not _claude_list_models_available(),
    reason="claude list-models not available",
)
def test_integration_validate_config_happy_path():
    from pycastle.validate import _fetch_models, validate_config

    _fetch_models.cache_clear()
    overrides = {
        "plan": {"model": "sonnet", "effort": "low"},
        "implement": {"model": "opus", "effort": "high"},
        "review": {"model": "haiku", "effort": "normal"},
        "merge": {"model": "", "effort": ""},
    }
    validate_config(overrides)
    assert overrides["plan"]["model"].startswith("claude-sonnet-")
    assert overrides["implement"]["model"].startswith("claude-opus-")
    assert overrides["review"]["model"].startswith("claude-haiku-")
    assert overrides["merge"]["model"] == ""
    assert overrides["plan"]["effort"] == "low"
    assert overrides["merge"]["effort"] == ""
