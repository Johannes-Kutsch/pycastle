import ast
from pathlib import Path

import pytest


def _layout():
    from pycastle.init_wizard import InitWizardLayoutFacts

    return InitWizardLayoutFacts(
        pycastle_dir=Path("pycastle"),
        pycastle_home=Path("/tmp/home"),
        target_config_file=Path("pycastle/config.py"),
        target_env_file=Path("pycastle/.env"),
        local_env_file=Path("pycastle/.env"),
        global_env_file=Path("/tmp/home/.env"),
    )


def test_init_wizard_exports_init_plan_public_names():
    from pycastle import init_wizard

    assert init_wizard.__all__ == [
        "ConfigFileAction",
        "ConfigHintAction",
        "CredentialPrompt",
        "EnvKeyAction",
        "EnvKeyActionKind",
        "HostAuthFacts",
        "InitPlan",
        "InitWizardLayoutFacts",
        "InitWizardPlanningInputs",
        "InitWizardScopeChoice",
        "LabelPromptEligibility",
        "PlannedEnvFile",
        "PlannedWarning",
        "ScaffoldStageChainFacts",
        "build_init_plan",
    ]


def test_init_plan_types_capture_init_wizard_planning_facts():
    from pycastle.init_wizard import (
        ConfigFileAction,
        ConfigHintAction,
        CredentialPrompt,
        EnvKeyAction,
        HostAuthFacts,
        InitPlan,
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        LabelPromptEligibility,
        PlannedEnvFile,
        PlannedWarning,
        ScaffoldStageChainFacts,
    )

    layout = InitWizardLayoutFacts(
        pycastle_dir=Path("pycastle"),
        pycastle_home=Path("/tmp/home"),
        target_config_file=Path("pycastle/config.py"),
        target_env_file=Path("pycastle/.env"),
        local_env_file=Path("pycastle/.env"),
        global_env_file=Path("/tmp/home/.env"),
    )
    planning_inputs = InitWizardPlanningInputs(
        selected_services=("claude", "codex"),
        scope_choice="local",
        layout=layout,
        existing_env_values={"GH_TOKEN": "present"},
        host_auth=HostAuthFacts(has_host_codex_auth=True),
        scaffold_stage_chains=ScaffoldStageChainFacts(
            bundled_default_stage_chains=(("plan", "review"),)
        ),
    )
    init_plan = InitPlan(
        selected_services=planning_inputs.selected_services,
        scope_choice=planning_inputs.scope_choice,
        target_config_file=layout.target_config_file,
        planned_env_file=PlannedEnvFile(
            path=layout.target_env_file,
            should_manage=True,
            missing_keys=("CLAUDE_CODE_OAUTH_TOKEN",),
        ),
        env_key_actions=(EnvKeyAction(key="GH_TOKEN", action="keep"),),
        credential_prompts=(
            CredentialPrompt(
                key="CLAUDE_CODE_OAUTH_TOKEN",
                prompt_text="Claude OAuth token",
                allow_overwrite=False,
            ),
        ),
        warnings=(PlannedWarning(message="warning"),),
        config_file_action=ConfigFileAction(
            path=layout.target_config_file,
            should_create=True,
            hints=(ConfigHintAction(key="service", value="claude"),),
        ),
        label_prompt_eligibility=LabelPromptEligibility(
            should_prompt=True,
            reason="GH_TOKEN present",
        ),
    )

    assert init_plan == InitPlan(
        selected_services=("claude", "codex"),
        scope_choice="local",
        target_config_file=Path("pycastle/config.py"),
        planned_env_file=PlannedEnvFile(
            path=Path("pycastle/.env"),
            should_manage=True,
            missing_keys=("CLAUDE_CODE_OAUTH_TOKEN",),
        ),
        env_key_actions=(EnvKeyAction(key="GH_TOKEN", action="keep"),),
        credential_prompts=(
            CredentialPrompt(
                key="CLAUDE_CODE_OAUTH_TOKEN",
                prompt_text="Claude OAuth token",
                allow_overwrite=False,
            ),
        ),
        warnings=(PlannedWarning(message="warning"),),
        config_file_action=ConfigFileAction(
            path=Path("pycastle/config.py"),
            should_create=True,
            hints=(ConfigHintAction(key="service", value="claude"),),
        ),
        label_prompt_eligibility=LabelPromptEligibility(
            should_prompt=True,
            reason="GH_TOKEN present",
        ),
    )


def test_init_wizard_planning_module_has_no_click_imports():
    planning_source = Path("src/pycastle/init_wizard/planning.py").read_text()
    module = ast.parse(planning_source)

    imported_modules = {
        alias.name
        for node in module.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in module.body if isinstance(node, ast.ImportFrom)
    )

    assert "click" not in imported_modules


@pytest.mark.parametrize(
    ("selected_services", "expected_services"),
    [
        ((), ("claude", "codex", "opencode")),
        (("",), ("claude", "codex", "opencode")),
        (("claude",), ("claude",)),
        ((" CLAUDE ",), ("claude",)),
        (("codex",), ("codex",)),
        (("opencode",), ("opencode",)),
        ((" OpEnCoDe ",), ("opencode",)),
        (("all",), ("claude", "codex", "opencode")),
        ((" ALL ",), ("claude", "codex", "opencode")),
    ],
    ids=[
        "empty_tuple_defaults_to_all",
        "empty_string_defaults_to_all",
        "claude",
        "claude_strips_and_lowercases",
        "codex",
        "opencode",
        "opencode_strips_and_lowercases",
        "all",
        "all_strips_and_lowercases",
    ],
)
def test_build_init_plan_normalizes_service_selection_aliases(
    selected_services, expected_services
):
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=selected_services,
            scope_choice="local",
            layout=_layout(),
        )
    )

    assert plan.selected_services == expected_services


def test_build_init_plan_rejects_invalid_service_selection_with_init_wording():
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    with pytest.raises(
        ValueError,
        match="Invalid service selection 'both'\\. Choose one of: claude/codex/opencode/all\\.",
    ):
        build_init_plan(
            InitWizardPlanningInputs(
                selected_services=("both",),
                scope_choice="local",
                layout=_layout(),
            )
        )


@pytest.mark.parametrize(
    (
        "selected_services",
        "expected_missing_keys",
        "expected_actions",
        "expected_prompt_keys",
    ),
    [
        (
            ("all",),
            (
                "GH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENCODE_GO_API_KEY",
            ),
            (
                ("GH_TOKEN", "prompt"),
                ("CLAUDE_CODE_OAUTH_TOKEN", "prompt"),
                ("OPENCODE_GO_API_KEY", "prompt"),
            ),
            ("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENCODE_GO_API_KEY"),
        ),
        (
            ("claude",),
            ("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
            (
                ("GH_TOKEN", "prompt"),
                ("CLAUDE_CODE_OAUTH_TOKEN", "prompt"),
            ),
            ("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        ),
        (
            ("codex",),
            ("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
            (
                ("GH_TOKEN", "prompt"),
                ("CLAUDE_CODE_OAUTH_TOKEN", "add_missing"),
            ),
            ("GH_TOKEN",),
        ),
        (
            ("opencode",),
            (
                "GH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENCODE_GO_API_KEY",
            ),
            (
                ("GH_TOKEN", "prompt"),
                ("CLAUDE_CODE_OAUTH_TOKEN", "add_missing"),
                ("OPENCODE_GO_API_KEY", "prompt"),
            ),
            ("GH_TOKEN", "OPENCODE_GO_API_KEY"),
        ),
    ],
    ids=["all", "claude", "codex", "opencode"],
)
def test_build_init_plan_sets_managed_env_keys_and_credential_prompts_by_service(
    selected_services,
    expected_missing_keys,
    expected_actions,
    expected_prompt_keys,
):
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=selected_services,
            scope_choice="local",
            layout=_layout(),
        )
    )

    assert plan.planned_env_file.missing_keys == expected_missing_keys
    assert tuple((action.key, action.action) for action in plan.env_key_actions) == (
        expected_actions
    )
    assert (
        tuple(prompt.key for prompt in plan.credential_prompts) == expected_prompt_keys
    )


def test_build_init_plan_represents_existing_prompted_credentials_as_overwrite_choices():
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("all",),
            scope_choice="local",
            layout=_layout(),
            existing_env_values={
                "GH_TOKEN": "existing-gh",
                "CLAUDE_CODE_OAUTH_TOKEN": "existing-claude",
                "OPENCODE_GO_API_KEY": "existing-opencode",
            },
        )
    )

    assert plan.planned_env_file.missing_keys == ()
    assert tuple((action.key, action.action) for action in plan.env_key_actions) == (
        ("GH_TOKEN", "overwrite_prompt"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "overwrite_prompt"),
        ("OPENCODE_GO_API_KEY", "overwrite_prompt"),
    )
    assert tuple(
        (prompt.key, prompt.allow_overwrite) for prompt in plan.credential_prompts
    ) == (
        ("GH_TOKEN", True),
        ("CLAUDE_CODE_OAUTH_TOKEN", True),
        ("OPENCODE_GO_API_KEY", True),
    )


def test_build_init_plan_keeps_existing_empty_unprompted_env_keys():
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("codex",),
            scope_choice="local",
            layout=_layout(),
            existing_env_keys=("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
            existing_env_values={"GH_TOKEN": "existing-gh"},
        )
    )

    assert plan.planned_env_file.missing_keys == ()
    assert tuple((action.key, action.action) for action in plan.env_key_actions) == (
        ("GH_TOKEN", "overwrite_prompt"),
        ("CLAUDE_CODE_OAUTH_TOKEN", "keep"),
    )


def test_build_init_plan_is_filesystem_pure_for_service_selection_and_credentials(
    tmp_path,
):
    from pycastle.init_wizard import (
        HostAuthFacts,
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        ScaffoldStageChainFacts,
        build_init_plan,
    )

    pycastle_dir = tmp_path / "pycastle"
    env_file = pycastle_dir / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("GH_TOKEN=existing-gh\n")
    before = env_file.read_text()

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("codex",),
            scope_choice="local",
            layout=InitWizardLayoutFacts(
                pycastle_dir=pycastle_dir,
                pycastle_home=tmp_path / "home",
                target_config_file=pycastle_dir / "config.py",
                target_env_file=env_file,
                local_env_file=env_file,
                global_env_file=tmp_path / "home" / ".env",
            ),
            existing_env_values={"GH_TOKEN": "existing-gh"},
            host_auth=HostAuthFacts(has_host_codex_auth=False),
            scaffold_stage_chains=ScaffoldStageChainFacts(),
        )
    )

    assert plan.selected_services == ("codex",)
    assert env_file.read_text() == before
