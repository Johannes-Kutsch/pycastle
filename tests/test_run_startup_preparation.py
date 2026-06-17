from pycastle.config import Config, StageOverride
from pycastle.run_startup_preparation import (
    RunStartupImproveModeFlagFacts,
    prepare_run_startup,
)
from pycastle.services.opencode_service import OpenCodeService
from unittest.mock import patch


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
