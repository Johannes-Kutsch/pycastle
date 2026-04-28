from unittest.mock import MagicMock

import pytest

from pycastle.claude_service import ClaudeService
from pycastle.errors import (
    ClaudeCliNotFoundError,
    ClaudeCommandError,
    ClaudeServiceError,
    ClaudeTimeoutError,
    ConfigValidationError,
    PycastleError,
)


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


def _make_service(models: list[str] = _FAKE_MODELS) -> ClaudeService:
    mock = MagicMock(spec=ClaudeService)
    mock.list_models.return_value = tuple(models)
    return mock


@pytest.fixture(autouse=True)
def _clear_model_cache():
    from pycastle.validate import _fetch_models

    _fetch_models.cache_clear()
    yield
    _fetch_models.cache_clear()


# ── validate_config: empty overrides are a no-op ─────────────────────────────


def test_empty_overrides_do_not_call_claude():
    from pycastle.validate import validate_config

    mock_service = _make_service()
    validate_config({}, claude_service=mock_service)
    mock_service.list_models.assert_not_called()


# ── validate_config: empty model/effort strings pass without modification ─────


def test_empty_model_skips_validation_and_leaves_value():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "", "effort": ""}}
    validate_config(overrides, claude_service=_make_service())
    assert overrides["plan"]["model"] == ""


def test_empty_effort_skips_validation_and_leaves_value():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "", "effort": ""}}
    validate_config(overrides, claude_service=_make_service())
    assert overrides["plan"]["effort"] == ""


# ── validate_config: valid shorthand resolves to full model ID ────────────────


def test_valid_shorthand_resolves_to_full_model_id():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    validate_config(overrides, claude_service=_make_service())
    assert overrides["plan"]["model"] == "claude-sonnet-4-6"


def test_haiku_shorthand_resolves():
    from pycastle.validate import validate_config

    overrides = {"implement": {"model": "haiku", "effort": ""}}
    validate_config(overrides, claude_service=_make_service())
    assert overrides["implement"]["model"] == "claude-haiku-4-5-20251001"


def test_opus_shorthand_resolves():
    from pycastle.validate import validate_config

    overrides = {"review": {"model": "opus", "effort": ""}}
    validate_config(overrides, claude_service=_make_service())
    assert overrides["review"]["model"] == "claude-opus-4-7"


# ── validate_config: highest semver wins when multiple versions exist ─────────


def test_highest_semver_wins_for_shorthand():
    from pycastle.validate import validate_config

    models = [
        "claude-sonnet-3-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5-20241022",
    ]
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    validate_config(overrides, claude_service=_make_service(models))
    assert overrides["plan"]["model"] == "claude-sonnet-4-6"


def test_newest_patch_wins_over_older_minor():
    from pycastle.validate import validate_config

    models = [
        "claude-haiku-3-5",
        "claude-haiku-4-5-20251001",
    ]
    overrides = {"plan": {"model": "haiku", "effort": ""}}
    validate_config(overrides, claude_service=_make_service(models))
    assert overrides["plan"]["model"] == "claude-haiku-4-5-20251001"


# ── validate_config: invalid model raises ConfigValidationError ───────────────


def test_invalid_model_raises_config_validation_error():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "gpt4", "effort": ""}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service())
    assert exc_info.value.invalid_value == "gpt4"


def test_invalid_model_error_has_suggestion():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "sonnit", "effort": ""}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service())
    assert exc_info.value.suggestion == "sonnet"


def test_invalid_model_error_lists_valid_options():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "unknown", "effort": ""}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service())
    assert set(exc_info.value.valid_options) == {"haiku", "sonnet", "opus"}


# ── validate_config: invalid effort raises ConfigValidationError ─────────────


def test_invalid_effort_raises_config_validation_error():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "", "effort": "ultra"}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service())
    assert exc_info.value.invalid_value == "ultra"


def test_invalid_effort_error_has_suggestion():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "", "effort": "hih"}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service())
    assert exc_info.value.suggestion == "high"


def test_invalid_effort_error_lists_valid_options():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "", "effort": "ultra"}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service())
    assert set(exc_info.value.valid_options) == {
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }


def test_valid_effort_values_pass():
    from pycastle.validate import _fetch_models, validate_config

    for effort in ("low", "medium", "high", "xhigh", "max"):
        _fetch_models.cache_clear()
        overrides = {"plan": {"model": "", "effort": effort}}
        validate_config(overrides, claude_service=_make_service())
        assert overrides["plan"]["effort"] == effort


# ── validate_config: ClaudeService errors convert to ConfigValidationError ────


def test_cli_not_found_raises_config_validation_error():
    from pycastle.validate import validate_config

    mock_service = MagicMock(spec=ClaudeService)
    mock_service.list_models.side_effect = ClaudeCliNotFoundError(
        "claude CLI not found; ensure it is installed and on PATH"
    )
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=mock_service)
    assert "claude" in str(exc_info.value).lower()


def test_nonzero_exit_raises_config_validation_error():
    from pycastle.validate import validate_config

    mock_service = MagicMock(spec=ClaudeService)
    mock_service.list_models.side_effect = ClaudeCommandError(
        "claude list-models failed (exit 127): not found"
    )
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with pytest.raises(ConfigValidationError):
        validate_config(overrides, claude_service=mock_service)


def test_timeout_raises_config_validation_error():
    from pycastle.validate import validate_config

    mock_service = MagicMock(spec=ClaudeService)
    mock_service.list_models.side_effect = ClaudeTimeoutError(
        "claude list-models timed out after 10 s"
    )
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with pytest.raises(ConfigValidationError):
        validate_config(overrides, claude_service=mock_service)


def test_claude_service_error_converts_to_config_validation_error():
    from pycastle.validate import validate_config

    mock_service = MagicMock(spec=ClaudeService)
    mock_service.list_models.side_effect = ClaudeServiceError(
        "claude list-models returned no models"
    )
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with pytest.raises(ConfigValidationError):
        validate_config(overrides, claude_service=mock_service)


# ── validate_config: ClaudeServiceError message is preserved ─────────────────


def test_claude_service_error_message_is_preserved_in_config_validation_error():
    from pycastle.validate import validate_config

    mock_service = MagicMock(spec=ClaudeService)
    mock_service.list_models.side_effect = ClaudeServiceError(
        "very specific error text"
    )
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=mock_service)
    assert "very specific error text" in str(exc_info.value)


# ── validate_config: list-models is only called once (cached) ─────────────────


def test_list_models_called_once_across_multiple_validations():
    from pycastle.validate import validate_config

    mock_service = _make_service()
    overrides1 = {"plan": {"model": "sonnet", "effort": ""}}
    overrides2 = {"implement": {"model": "haiku", "effort": ""}}
    validate_config(overrides1, claude_service=mock_service)
    validate_config(overrides2, claude_service=mock_service)
    mock_service.list_models.assert_called_once()


def test_different_service_instances_cache_independently():
    from pycastle.validate import validate_config

    service1 = _make_service(["claude-sonnet-4-6"])
    service2 = _make_service(["claude-haiku-4-5-20251001"])
    validate_config(
        {"plan": {"model": "sonnet", "effort": ""}}, claude_service=service1
    )
    validate_config({"plan": {"model": "haiku", "effort": ""}}, claude_service=service2)
    service1.list_models.assert_called_once()
    service2.list_models.assert_called_once()


def test_service_error_is_not_cached_subsequent_call_succeeds():
    from pycastle.validate import validate_config

    mock_service = MagicMock(spec=ClaudeService)
    mock_service.list_models.side_effect = ClaudeServiceError("transient failure")
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with pytest.raises(ConfigValidationError):
        validate_config(overrides, claude_service=mock_service)

    mock_service.list_models.side_effect = None
    mock_service.list_models.return_value = tuple(_FAKE_MODELS)
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    validate_config(overrides, claude_service=mock_service)
    assert overrides["plan"]["model"] == "claude-sonnet-4-6"
    assert mock_service.list_models.call_count == 2


# ── validate_config: atomicity — no partial mutations on failure ──────────────


def test_failed_validation_does_not_mutate_earlier_resolved_stages():
    from pycastle.validate import validate_config

    overrides = {
        "plan": {"model": "sonnet", "effort": ""},  # valid — resolved first
        "implement": {"model": "badmodel", "effort": ""},  # invalid — raises second
    }
    with pytest.raises(ConfigValidationError):
        validate_config(overrides, claude_service=_make_service())
    assert overrides["plan"]["model"] == "sonnet"


# ── validate_config: claude is not called when all model strings are empty ────


def test_stages_with_only_empty_models_do_not_call_claude():
    from pycastle.validate import validate_config

    mock_service = _make_service()
    overrides = {
        "plan": {"model": "", "effort": "low"},
        "implement": {"model": "", "effort": ""},
    }
    validate_config(overrides, claude_service=mock_service)
    mock_service.list_models.assert_not_called()


# ── validate_config: no parseable claude models in output ────────────────────


def test_no_parseable_claude_models_raises_with_empty_valid_options():
    from pycastle.validate import validate_config

    non_claude_models = ["gpt-4", "gpt-3.5-turbo"]
    overrides = {"plan": {"model": "sonnet", "effort": ""}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service(non_claude_models))
    assert exc_info.value.invalid_value == "sonnet"
    assert exc_info.value.valid_options == []


# ── validate_config: idempotency — calling twice on the same dict is safe ─────


def test_validate_config_is_idempotent_with_shorthand():
    from pycastle.validate import validate_config

    svc = _make_service()
    overrides = {"plan": {"model": "haiku", "effort": ""}}
    validate_config(overrides, claude_service=svc)
    # First call resolves "haiku" → full ID in place; second call must not raise.
    validate_config(overrides, claude_service=svc)
    assert overrides["plan"]["model"] == "claude-haiku-4-5-20251001"


def test_already_resolved_full_model_id_is_accepted_unchanged():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "claude-sonnet-4-6", "effort": ""}}
    validate_config(overrides, claude_service=_make_service())
    assert overrides["plan"]["model"] == "claude-sonnet-4-6"


def test_unknown_full_looking_id_still_raises():
    from pycastle.validate import validate_config

    overrides = {"plan": {"model": "claude-haiku-99-0", "effort": ""}}
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_config(overrides, claude_service=_make_service())
    assert exc_info.value.invalid_value == "claude-haiku-99-0"
