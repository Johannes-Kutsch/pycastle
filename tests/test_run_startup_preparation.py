from pycastle.config import Config, StageOverride
from pycastle.run_startup_preparation import (
    RunStartupImproveModeFlagFacts,
    prepare_run_startup,
)


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
    assert startup.shared_container_env == {
        "GH_TOKEN": "gh",
        "CLAUDE_CODE_OAUTH_TOKEN": "primary",
    }
    assert startup.effective_improve_mode == "until_sleep"


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

    assert startup.validation_failures == ("  stage='plan': service is required",)


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

    assert startup.validation_failures == (
        "  stage='implement': no locally configured service in priority chain "
        "'claude -> opencode'",
    )
    assert startup.effective_improve_mode == "endless"
