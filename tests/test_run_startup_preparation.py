from typing import cast

from pycastle.services.service_registry import ServiceRegistry

from pycastle.config import Config, StageOverride
from pycastle.run_startup_preparation import (
    RunStartupImproveModeFlagFacts,
    RunStartupPreparation,
    StageOverrideValidationFailure,
    prepare_run_startup,
)
from pycastle.services.runtime_services import ClaudeService, CodexService
from pycastle.services.runtime_services import OpenCodeService
from unittest.mock import patch


def test_run_startup_preparation_returns_none_validation_error_message_without_failures():
    startup = RunStartupPreparation(
        validation_failures=(),
        configured_provider_adapters={},
        runtime_registry=cast(ServiceRegistry, object()),
        shared_container_env={},
        effective_improve_mode=None,
    )

    assert startup.validation_error_message is None


def test_run_startup_preparation_renders_validation_error_message_in_existing_cli_format():
    startup = RunStartupPreparation(
        validation_failures=(
            StageOverrideValidationFailure(
                code="missing_service",
                stage_label="plan",
            ),
            StageOverrideValidationFailure(
                code="missing_effort",
                stage_label="implement",
            ),
        ),
        configured_provider_adapters={},
        runtime_registry=cast(ServiceRegistry, object()),
        shared_container_env={},
        effective_improve_mode=None,
    )

    assert startup.validation_error_message == (
        "Config validation errors:\n"
        "  stage='plan': service is required\n"
        "  stage='implement': effort is required"
    )


def test_prepare_run_startup_uses_none_effective_improve_mode_when_unset_and_unflagged():
    startup = prepare_run_startup(
        Config(docker_image_name="img"),
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.effective_improve_mode is None


def test_prepare_run_startup_uses_config_effective_improve_mode_when_unflagged():
    startup = prepare_run_startup(
        Config(docker_image_name="img", improve_mode="endless"),
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.effective_improve_mode == "endless"


def test_prepare_run_startup_explicit_improve_flag_overrides_config():
    startup = prepare_run_startup(
        Config(docker_image_name="img", improve_mode="endless"),
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag="until_sleep",
        ),
    )

    assert startup.effective_improve_mode == "until_sleep"


def test_prepare_run_startup_explicit_no_improve_overrides_other_improve_facts():
    startup = prepare_run_startup(
        Config(docker_image_name="img", improve_mode="endless"),
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=True,
            improve_mode_flag="until_sleep",
        ),
    )

    assert startup.effective_improve_mode is None


def test_prepare_run_startup_uses_only_passed_improve_flag_facts(monkeypatch):
    def _unexpected_click_state() -> None:
        raise AssertionError("prepare_run_startup must not read Click state")

    monkeypatch.setattr("click.get_current_context", _unexpected_click_state)

    startup = prepare_run_startup(
        Config(docker_image_name="img", improve_mode="endless"),
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=True,
            improve_mode_flag=None,
        ),
    )

    assert startup.effective_improve_mode is None


def test_prepare_run_startup_returns_explicit_startup_preparation_fields():
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN": "primary",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary",
            "OPENCODE_GO_API_KEY": "opencode",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.validation_failures == ()
    assert startup.configured_provider_adapters.keys() == {
        "claude",
        "codex",
        "opencode",
    }
    assert startup.runtime_registry.services == startup.configured_provider_adapters
    assert startup.shared_container_env == {"GH_TOKEN": "gh"}
    assert startup.effective_improve_mode == "until_sleep"


def test_prepare_run_startup_preserves_passed_shared_container_credential_facts():
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "SHARED_CONTAINER_TOKEN": "shared",
            "CLAUDE_CODE_OAUTH_TOKEN": "primary",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary",
            "OPENCODE_GO_API_KEY": "opencode",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.shared_container_env == {
        "GH_TOKEN": "gh",
        "SHARED_CONTAINER_TOKEN": "shared",
    }


def test_prepare_run_startup_keeps_provider_credentials_behind_provider_adapters():
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN": "primary",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary",
            "OPENCODE_GO_API_KEY": "opencode",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    claude = startup.configured_provider_adapters["claude"]
    opencode = startup.configured_provider_adapters["opencode"]

    assert isinstance(claude, ClaudeService)
    assert claude.account_names() == ["account 1"]
    assert claude.build_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "primary"
    assert opencode.build_env()["OPENCODE_GO_API_KEY"] == "opencode"
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in startup.shared_container_env
    assert "OPENCODE_GO_API_KEY" not in startup.shared_container_env


def test_prepare_run_startup_passes_openai_api_key_to_codex_service_and_excludes_from_shared_env():
    # OPENAI_API_KEY must go into the container via CodexService.build_env() so that
    # Codex has it when launched through `docker exec` (issue #1894).
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "OPENAI_API_KEY": "sk-test",
        },
        RunStartupImproveModeFlagFacts(no_improve=False, improve_mode_flag=None),
    )

    codex = startup.configured_provider_adapters["codex"]
    assert isinstance(codex, CodexService)
    assert codex.build_env()["OPENAI_API_KEY"] == "sk-test"
    assert "OPENAI_API_KEY" not in startup.shared_container_env


def test_prepare_run_startup_uses_numbered_opencode_credentials_in_priority_order():
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "OPENCODE_GO_API_KEY": "slot-1",
            "OPENCODE_GO_API_KEY_2": "slot-2",
            "OPENCODE_GO_API_KEY_10": "slot-10",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    opencode = startup.configured_provider_adapters["opencode"]

    assert opencode.account_names() == ["account 1", "account 2", "account 10"]
    assert opencode.build_env()["OPENCODE_GO_API_KEY"] == "slot-1"


def test_prepare_run_startup_keeps_single_bare_opencode_key_behavior_unchanged():
    startup = prepare_run_startup(
        Config(docker_image_name="img", improve_mode="until_sleep"),
        {
            "GH_TOKEN": "gh",
            "OPENCODE_GO_API_KEY": "single-key",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    opencode = startup.configured_provider_adapters["opencode"]

    assert opencode.account_names() == ["account 1"]
    assert opencode.build_env()["OPENCODE_GO_API_KEY"] == "single-key"


def test_prepare_run_startup_prefers_bare_claude_credential_over_numbered_slot_two():
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN": "slot-1",
            "CLAUDE_CODE_OAUTH_TOKEN_2": "slot-2",
            "CLAUDE_CODE_OAUTH_TOKEN_10": "slot-10",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    claude = startup.configured_provider_adapters["claude"]

    assert claude.account_names() == ["account 1", "account 2", "account 10"]
    assert claude.build_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "slot-1"


def test_prepare_run_startup_omits_numbered_claude_credentials_from_shared_container_env():
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN": "slot-1",
            "CLAUDE_CODE_OAUTH_TOKEN_2": "slot-2",
            "CLAUDE_CODE_OAUTH_TOKEN_10": "slot-10",
            "SHARED_CONTAINER_TOKEN": "shared",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.shared_container_env == {
        "GH_TOKEN": "gh",
        "SHARED_CONTAINER_TOKEN": "shared",
    }


def test_prepare_run_startup_uses_only_passed_credential_env_facts(monkeypatch):
    cfg = Config(docker_image_name="img", improve_mode="until_sleep")
    monkeypatch.setenv("GH_TOKEN", "ambient-gh")
    monkeypatch.setenv("SHARED_CONTAINER_TOKEN", "ambient-shared")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-primary")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "ambient-secondary")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "ambient-opencode")

    startup = prepare_run_startup(
        cfg,
        {},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.shared_container_env == {}
    assert startup.configured_provider_adapters.keys() == {"codex"}


def test_prepare_run_startup_rejects_slot_1_conflict_between_bare_and_prefixed_key():
    startup = prepare_run_startup(
        Config(docker_image_name="img"),
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "primary",
            "CLAUDE_CODE_OAUTH_TOKEN_1": "slot-1",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.validation_error_message is not None
    assert "cannot resolve slot 1" in startup.validation_error_message
    assert (
        "CLAUDE_CODE_OAUTH_TOKEN and CLAUDE_CODE_OAUTH_TOKEN_1"
        in startup.validation_error_message
    )


def test_prepare_run_startup_omits_services_absent_from_resolved_stage_chains():
    codex = StageOverride(service="codex", model="gpt-5.4", effort="medium")
    cfg = Config(
        docker_image_name="img",
        plan_override=codex,
        implement_override=codex,
        review_override=codex,
        merge_override=codex,
        preflight_issue_override=codex,
        improve_override=codex,
    )

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN": "primary",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary",
            "OPENCODE_GO_API_KEY": "opencode",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.validation_failures == ()
    assert startup.configured_provider_adapters.keys() == {"codex"}
    assert startup.runtime_registry.services == startup.configured_provider_adapters


def test_prepare_run_startup_default_stage_chains_keep_remaining_configured_services():
    startup = prepare_run_startup(
        Config(docker_image_name="img"),
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.validation_failures == ()
    assert startup.configured_provider_adapters.keys() == {"codex"}
    assert startup.runtime_registry.services == startup.configured_provider_adapters


def test_prepare_run_startup_accepts_opencode_priority_chain_when_claude_fallback_is_configured():
    opencode_then_claude = StageOverride(
        service="opencode",
        model="kimi-k2.6",
        effort="medium",
        fallback=StageOverride(service="claude", model="sonnet", effort="medium"),
    )
    cfg = Config(
        docker_image_name="img",
        plan_override=opencode_then_claude,
        implement_override=opencode_then_claude,
        review_override=opencode_then_claude,
        merge_override=opencode_then_claude,
        preflight_issue_override=opencode_then_claude,
        improve_override=opencode_then_claude,
    )

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN": "primary",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.validation_failures == ()
    assert startup.configured_provider_adapters.keys() == {"claude"}
    assert startup.runtime_registry.services == startup.configured_provider_adapters


def test_prepare_run_startup_requires_claude_primary_token_to_build_adapter():
    claude = StageOverride(service="claude", model="sonnet", effort="medium")
    cfg = Config(
        docker_image_name="img",
        plan_override=claude,
        implement_override=claude,
        review_override=claude,
        merge_override=claude,
        preflight_issue_override=claude,
        improve_override=claude,
    )

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.configured_provider_adapters == {}
    assert startup.runtime_registry.services == startup.configured_provider_adapters
    assert startup.shared_container_env == {"GH_TOKEN": "gh"}
    assert [failure.render() for failure in startup.validation_failures] == [
        "  stage='plan': no locally configured service in priority chain 'claude'",
        "  stage='implement': no locally configured service in priority chain 'claude'",
        "  stage='review': no locally configured service in priority chain 'claude'",
        "  stage='merge': no locally configured service in priority chain 'claude'",
        "  stage='preflight_issue': no locally configured service in priority chain 'claude'",
        "  stage='improve': no locally configured service in priority chain 'claude'",
    ]


def test_prepare_run_startup_ignores_empty_and_repeated_stage_chain_service_names():
    repeated_claude_chain = StageOverride(
        service=" ",
        fallback=StageOverride(
            service="claude",
            fallback=StageOverride(
                service="claude",
                fallback=StageOverride(service="opencode"),
            ),
        ),
    )
    cfg = Config(
        docker_image_name="img",
        plan_override=repeated_claude_chain,
        implement_override=repeated_claude_chain,
        review_override=repeated_claude_chain,
        merge_override=repeated_claude_chain,
        preflight_issue_override=repeated_claude_chain,
        improve_override=repeated_claude_chain,
    )

    startup = prepare_run_startup(
        cfg,
        {
            "GH_TOKEN": "gh",
            "CLAUDE_CODE_OAUTH_TOKEN": "primary",
        },
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert startup.configured_provider_adapters.keys() == {"claude"}
    assert startup.runtime_registry.services == startup.configured_provider_adapters


def test_prepare_run_startup_short_circuits_before_local_priority_chain_validation():
    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="", effort="low"),
    )

    startup = prepare_run_startup(
        cfg,
        {},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert [failure.render() for failure in startup.validation_failures] == [
        "  stage='plan': service is required"
    ]


def test_prepare_run_startup_returns_structured_stage_override_validation_failures():
    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="", effort=""),
        implement_override=StageOverride(service="codez", effort="medium"),
        review_override=StageOverride(service="codex", model="gpt-5.4", effort="max"),
        merge_override=StageOverride(
            service="codex", model="gpt-5.4-min", effort="medium"
        ),
        preflight_issue_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
            fallback=StageOverride(
                service="opencode",
                model="gpt-5.4",
                effort="medium",
            ),
        ),
    )

    startup = prepare_run_startup(
        cfg,
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert [failure.code for failure in startup.validation_failures] == [
        "missing_service",
        "missing_effort",
        "unknown_service",
        "invalid_effort",
        "invalid_model",
        "invalid_model",
    ]
    assert [failure.stage_label for failure in startup.validation_failures] == [
        "plan",
        "plan",
        "implement",
        "review",
        "merge",
        "preflight_issue fallback",
    ]
    assert [failure.render() for failure in startup.validation_failures] == [
        "  stage='plan': service is required",
        "  stage='plan': effort is required",
        "  stage='implement': service='codez' is not a known service"
        " (known: ['claude', 'codex', 'opencode'])",
        "  stage='review': effort='max' is invalid for service='codex'"
        " (valid: ['high', 'low', 'medium', 'xhigh'])",
        "  stage='merge': model='gpt-5.4-min' is invalid for service='codex'."
        ' Did you mean "gpt-5.4-mini"?',
        "  stage='preflight_issue fallback': model='gpt-5.4' is invalid"
        f" for service='opencode'. (valid: "
        f"{sorted(OpenCodeService().valid_models())!r})",
    ]


def test_prepare_run_startup_reports_missing_configured_provider_adapter_in_priority_chain():
    absent_chain = StageOverride(
        service="claude",
        model="sonnet",
        effort="medium",
        fallback=StageOverride(service="opencode", model="kimi-k2.6", effort="medium"),
    )
    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
        implement_override=absent_chain,
        review_override=StageOverride(
            service="codex", model="gpt-5.4", effort="medium"
        ),
        merge_override=StageOverride(service="codex", model="gpt-5.4", effort="medium"),
        preflight_issue_override=StageOverride(
            service="codex", model="gpt-5.4", effort="medium"
        ),
        improve_override=StageOverride(
            service="codex", model="gpt-5.4", effort="medium"
        ),
    )

    startup = prepare_run_startup(
        cfg,
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag="endless",
        ),
    )

    assert [failure.render() for failure in startup.validation_failures] == [
        "  stage='implement': no locally configured service in priority chain "
        "'claude -> opencode'"
    ]
    assert startup.effective_improve_mode == "endless"


def test_prepare_run_startup_accepts_priority_chain_when_later_service_is_configured():
    class _ConfiguredCodexAdapter:
        def valid_models(self) -> frozenset[str]:
            return frozenset({"gpt-5.4"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
            fallback=StageOverride(
                service="codex",
                model="gpt-5.4",
                effort="medium",
            ),
        ),
        implement_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        review_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        merge_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        preflight_issue_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        improve_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
    )

    with patch(
        "pycastle.run_startup_preparation.configured_provider_adapters_for_run",
        return_value={"codex": _ConfiguredCodexAdapter()},
    ):
        startup = prepare_run_startup(
            cfg,
            {"GH_TOKEN": "gh"},
            RunStartupImproveModeFlagFacts(
                no_improve=False,
                improve_mode_flag=None,
            ),
        )

    assert startup.validation_failures == ()


def test_prepare_run_startup_returns_structured_provider_model_mismatch_failure():
    class _AcceptingAdapter:
        def __init__(self, models: frozenset[str]) -> None:
            self._models = models

        def valid_models(self) -> frozenset[str]:
            return self._models

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    class _ConfiguredAdapter:
        def valid_models(self) -> frozenset[str]:
            return frozenset({"gpt-5.4"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
            fallback=StageOverride(
                service="codex",
                model="gpt-5.4-mini",
                effort="medium",
            ),
        ),
        implement_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        review_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        merge_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        preflight_issue_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
        improve_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
        ),
    )

    with patch(
        "pycastle.run_startup_preparation.configured_provider_adapters_for_run",
        return_value={
            "claude": _AcceptingAdapter(frozenset({"sonnet"})),
            "codex": _ConfiguredAdapter(),
            "opencode": _AcceptingAdapter(frozenset({"deepseek-v4-flash"})),
        },
    ):
        startup = prepare_run_startup(
            cfg,
            {"GH_TOKEN": "gh"},
            RunStartupImproveModeFlagFacts(
                no_improve=False,
                improve_mode_flag=None,
            ),
        )

    assert [failure.code for failure in startup.validation_failures] == [
        "provider_model_mismatch"
    ]
    assert [failure.stage_label for failure in startup.validation_failures] == [
        "plan fallback"
    ]
    assert [failure.render() for failure in startup.validation_failures] == [
        "  stage='plan fallback': model='gpt-5.4-mini' is invalid for "
        "service='codex'. Did you mean \"gpt-5.4\"?"
    ]


def test_prepare_run_startup_reports_local_configured_chain_failures_alongside_provider_mismatches():
    class _ConfiguredCodexAdapter:
        def valid_models(self) -> frozenset[str]:
            return frozenset({"gpt-5.4"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    absent_chain = StageOverride(
        service="claude",
        model="sonnet",
        effort="medium",
        fallback=StageOverride(
            service="opencode",
            model="kimi-k2.6",
            effort="medium",
        ),
    )
    codex = StageOverride(service="codex", model="gpt-5.4", effort="medium")
    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(
            service="codex",
            model="gpt-5.4-mini",
            effort="medium",
        ),
        implement_override=absent_chain,
        review_override=codex,
        merge_override=codex,
        preflight_issue_override=codex,
        improve_override=codex,
    )

    with patch(
        "pycastle.run_startup_preparation.configured_provider_adapters_for_run",
        return_value={
            "codex": _ConfiguredCodexAdapter(),
        },
    ):
        startup = prepare_run_startup(
            cfg,
            {"GH_TOKEN": "gh"},
            RunStartupImproveModeFlagFacts(
                no_improve=False,
                improve_mode_flag=None,
            ),
        )

    assert [failure.code for failure in startup.validation_failures] == [
        "provider_model_mismatch",
        "no_configured_service",
    ]
    assert [failure.render() for failure in startup.validation_failures] == [
        "  stage='plan': model='gpt-5.4-mini' is invalid for service='codex'."
        ' Did you mean "gpt-5.4"?',
        "  stage='implement': no locally configured service in priority chain "
        "'claude -> opencode'",
    ]


def test_prepare_run_startup_preserves_full_priority_chain_label_for_repeated_services():
    cfg = Config(
        docker_image_name="img",
        plan_override=StageOverride(
            service="claude",
            model="sonnet",
            effort="medium",
            fallback=StageOverride(
                service="opencode",
                model="kimi-k2.6",
                effort="medium",
                fallback=StageOverride(
                    service="claude",
                    model="haiku",
                    effort="medium",
                ),
            ),
        ),
        implement_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        review_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        merge_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        preflight_issue_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
        improve_override=StageOverride(
            service="codex",
            model="gpt-5.4",
            effort="medium",
        ),
    )

    startup = prepare_run_startup(
        cfg,
        {"GH_TOKEN": "gh"},
        RunStartupImproveModeFlagFacts(
            no_improve=False,
            improve_mode_flag=None,
        ),
    )

    assert [failure.render() for failure in startup.validation_failures] == [
        "  stage='plan': no locally configured service in priority chain "
        "'claude -> opencode -> claude'"
    ]
