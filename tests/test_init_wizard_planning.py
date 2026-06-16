import ast
from pathlib import Path


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
