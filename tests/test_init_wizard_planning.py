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


def _layout_for_scope(scope: str):
    from pycastle.init_wizard import InitWizardLayoutFacts

    pycastle_dir = Path("pycastle")
    pycastle_home = Path("/tmp/home")
    scoped_dir = pycastle_home if scope == "global" else pycastle_dir
    return InitWizardLayoutFacts(
        pycastle_dir=pycastle_dir,
        pycastle_home=pycastle_home,
        target_config_file=scoped_dir / "config.py",
        target_env_file=scoped_dir / ".env",
        local_env_file=pycastle_dir / ".env",
        global_env_file=pycastle_home / ".env",
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
        "PlannedEnvFileAction",
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
        PlannedEnvFileAction,
        PlannedEnvFile,
        PlannedWarning,
        ScaffoldStageChainFacts,
    )

    planned_env_action: PlannedEnvFileAction = "merge"

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
            action=planned_env_action,
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
            action="merge",
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
    assert init_plan.warning_messages() == ("warning",)


def test_init_plan_warning_messages_preserve_warning_order():
    from pycastle.init_wizard import InitPlan, PlannedEnvFile, PlannedWarning

    plan = InitPlan(
        selected_services=("codex",),
        scope_choice="local",
        target_config_file=Path("pycastle/config.py"),
        planned_env_file=PlannedEnvFile(
            path=Path("pycastle/.env"),
            should_manage=True,
        ),
        warnings=(
            PlannedWarning(message="first warning"),
            PlannedWarning(message="second warning"),
        ),
    )

    assert plan.warning_messages() == ("first warning", "second warning")


def test_build_init_plan_local_missing_config_plans_creation_with_docker_image_hint(
    tmp_path, monkeypatch
):
    from pycastle.init_wizard import (
        ConfigHintAction,
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        build_init_plan,
    )

    repo_root = tmp_path / "My Cool Project"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)

    config_file = repo_root / "pycastle" / "config.py"

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="local",
            layout=InitWizardLayoutFacts(
                pycastle_dir=repo_root / "pycastle",
                pycastle_home=tmp_path / "home",
                target_config_file=config_file,
                target_env_file=repo_root / "pycastle" / ".env",
                local_env_file=repo_root / "pycastle" / ".env",
                global_env_file=tmp_path / "home" / ".env",
            ),
        )
    )

    assert plan.target_config_file == config_file
    assert plan.config_file_action is not None
    assert plan.config_file_action.path == config_file
    assert plan.config_file_action.should_create is True
    assert plan.config_file_action.hints == (
        ConfigHintAction(key="docker_image_name", value="my-cool-project"),
    )
    assert not config_file.exists()


def test_build_init_plan_global_missing_config_plans_creation_without_docker_image_hint(
    tmp_path, monkeypatch
):
    from pycastle.init_wizard import (
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        build_init_plan,
    )

    repo_root = tmp_path / "My Cool Project"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)

    config_file = tmp_path / "home" / "config.py"

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="global",
            layout=InitWizardLayoutFacts(
                pycastle_dir=repo_root / "pycastle",
                pycastle_home=tmp_path / "home",
                target_config_file=config_file,
                target_env_file=tmp_path / "home" / ".env",
                local_env_file=repo_root / "pycastle" / ".env",
                global_env_file=tmp_path / "home" / ".env",
            ),
        )
    )

    assert plan.target_config_file == config_file
    assert plan.config_file_action is not None
    assert plan.config_file_action.path == config_file
    assert plan.config_file_action.should_create is True
    assert plan.config_file_action.hints == ()
    assert not config_file.exists()


def test_build_init_plan_existing_global_config_preserves_file_with_untouched_message(
    tmp_path, monkeypatch
):
    from pycastle.init_wizard import (
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        build_init_plan,
    )

    repo_root = tmp_path / "project"
    repo_root.mkdir()
    monkeypatch.chdir(repo_root)

    config_file = tmp_path / "home" / "config.py"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("# existing\n")
    before = config_file.read_text()

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="global",
            layout=InitWizardLayoutFacts(
                pycastle_dir=repo_root / "pycastle",
                pycastle_home=tmp_path / "home",
                target_config_file=config_file,
                target_env_file=tmp_path / "home" / ".env",
                local_env_file=repo_root / "pycastle" / ".env",
                global_env_file=tmp_path / "home" / ".env",
            ),
        )
    )

    assert plan.config_file_action is not None
    assert plan.config_file_action.path == config_file
    assert plan.config_file_action.should_create is False
    assert plan.config_file_action.message == (
        f"global config.py already exists at {config_file}; leaving it untouched"
    )
    assert config_file.read_text() == before


def test_build_init_plan_marks_label_prompt_eligible_only_for_new_non_empty_gh_token():
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="local",
            layout=_layout(),
            manage_env_file=True,
            prompted_env_values={"GH_TOKEN": "new-gh-token"},
        )
    )

    assert plan.label_prompt_eligibility.should_prompt is True
    assert plan.label_prompt_eligibility.reason == "GH_TOKEN set during this init run"


@pytest.mark.parametrize(
    (
        "manage_env_file",
        "prompted_env_values",
        "existing_env_values",
        "expected_reason",
    ),
    [
        (
            False,
            {},
            {},
            "env management skipped for this init run",
        ),
        (
            True,
            {"GH_TOKEN": ""},
            {},
            "GH_TOKEN not set during this init run",
        ),
        (
            True,
            {},
            {"GH_TOKEN": "existing-gh-token"},
            "GH_TOKEN not set during this init run",
        ),
    ],
    ids=["env_management_skipped", "blank_prompted_token", "existing_token_preserved"],
)
def test_build_init_plan_keeps_label_prompt_ineligible_without_new_non_empty_gh_token(
    manage_env_file, prompted_env_values, existing_env_values, expected_reason
):
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="local",
            layout=_layout(),
            manage_env_file=manage_env_file,
            prompted_env_values=prompted_env_values,
            existing_env_values=existing_env_values,
        )
    )

    assert plan.label_prompt_eligibility.should_prompt is False
    assert plan.label_prompt_eligibility.reason == expected_reason


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


@pytest.mark.parametrize(
    ("scope_choice", "expected_config_file", "expected_env_file"),
    [
        ("global", Path("/tmp/home/config.py"), Path("/tmp/home/.env")),
        ("local", Path("pycastle/config.py"), Path("pycastle/.env")),
    ],
)
def test_build_init_plan_targets_scope_specific_config_and_env_files(
    scope_choice, expected_config_file, expected_env_file
):
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice=scope_choice,
            layout=_layout_for_scope(scope_choice),
        )
    )

    assert plan.target_config_file == expected_config_file
    assert plan.planned_env_file.path == expected_env_file


def test_build_init_plan_warns_when_codex_is_selected_without_host_auth():
    from pycastle.init_wizard import (
        HostAuthFacts,
        InitWizardPlanningInputs,
        PlannedWarning,
        build_init_plan,
    )

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("codex",),
            scope_choice="local",
            layout=_layout(),
            host_auth=HostAuthFacts(has_host_codex_auth=False),
        )
    )

    assert plan.warnings == (
        PlannedWarning(
            message="Codex authentication missing: run `codex login` on the host."
        ),
    )


@pytest.mark.parametrize(
    ("selected_services", "has_host_codex_auth"),
    [
        (("claude",), False),
        (("codex",), True),
    ],
    ids=["codex_not_selected", "host_auth_present"],
)
def test_build_init_plan_omits_codex_host_auth_warning_when_not_needed(
    selected_services, has_host_codex_auth
):
    from pycastle.init_wizard import (
        HostAuthFacts,
        InitWizardPlanningInputs,
        build_init_plan,
    )

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=selected_services,
            scope_choice="local",
            layout=_layout(),
            host_auth=HostAuthFacts(has_host_codex_auth=has_host_codex_auth),
        )
    )

    assert "Codex authentication missing: run `codex login` on the host." not in {
        warning.message for warning in plan.warnings
    }


def test_build_init_plan_warns_when_selected_services_do_not_cover_bundled_stage_chains():
    from pycastle.init_wizard import (
        InitWizardPlanningInputs,
        PlannedWarning,
        ScaffoldStageChainFacts,
        build_init_plan,
    )

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("opencode",),
            scope_choice="local",
            layout=_layout(),
            scaffold_stage_chains=ScaffoldStageChainFacts(
                bundled_default_stage_chains=(
                    ("opencode", "codex", "claude"),
                    ("codex", "claude"),
                )
            ),
        )
    )

    assert plan.warnings == (
        PlannedWarning(
            message=(
                "selected services do not cover every bundled default stage priority "
                "chain. Define your own stage overrides in config.py before running "
                "pycastle."
            )
        ),
    )


def test_build_init_plan_omits_bundled_stage_warning_when_selected_services_cover_every_chain():
    from pycastle.init_wizard import (
        InitWizardPlanningInputs,
        ScaffoldStageChainFacts,
        build_init_plan,
    )

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("codex",),
            scope_choice="local",
            layout=_layout(),
            scaffold_stage_chains=ScaffoldStageChainFacts(
                bundled_default_stage_chains=(
                    ("opencode", "codex", "claude"),
                    ("codex", "claude"),
                )
            ),
        )
    )

    assert (
        "selected services do not cover every bundled default stage priority chain"
        not in {warning.message for warning in plan.warnings}
    )


def test_build_init_plan_marks_delete_local_env_as_optional_when_global_scope_has_local_env(
    tmp_path,
):
    from pycastle.init_wizard import (
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        build_init_plan,
    )

    pycastle_dir = tmp_path / "pycastle"
    local_env_file = pycastle_dir / ".env"
    local_env_file.parent.mkdir(parents=True)
    local_env_file.write_text("GH_TOKEN=local-token\n")
    before = local_env_file.read_text()

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="global",
            layout=InitWizardLayoutFacts(
                pycastle_dir=pycastle_dir,
                pycastle_home=tmp_path / "home",
                target_config_file=tmp_path / "home" / "config.py",
                target_env_file=tmp_path / "home" / ".env",
                local_env_file=local_env_file,
                global_env_file=tmp_path / "home" / ".env",
            ),
        )
    )

    assert plan.planned_env_file.should_delete_local_env is True
    assert local_env_file.read_text() == before


@pytest.mark.parametrize(
    (
        "existing_env_keys",
        "existing_env_values",
        "expected_env_file_action",
        "expected_missing_keys",
    ),
    [
        ((), {}, "create", ("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")),
        (
            ("GH_TOKEN",),
            {"GH_TOKEN": "existing-gh"},
            "merge",
            ("CLAUDE_CODE_OAUTH_TOKEN",),
        ),
        (
            ("GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
            {
                "GH_TOKEN": "existing-gh",
                "CLAUDE_CODE_OAUTH_TOKEN": "existing-claude",
            },
            "preserve",
            (),
        ),
    ],
    ids=["create", "merge", "preserve"],
)
def test_build_init_plan_exposes_target_env_file_action(
    existing_env_keys,
    existing_env_values,
    expected_env_file_action,
    expected_missing_keys,
):
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="local",
            layout=_layout_for_scope("local"),
            existing_env_keys=existing_env_keys,
            existing_env_values=existing_env_values,
        )
    )

    assert plan.planned_env_file.action == expected_env_file_action
    assert plan.planned_env_file.missing_keys == expected_missing_keys


def test_build_init_plan_marks_create_local_env_as_optional_when_local_scope_has_only_global_env(
    tmp_path,
):
    from pycastle.init_wizard import (
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        build_init_plan,
    )

    pycastle_dir = tmp_path / "pycastle"
    local_env_file = pycastle_dir / ".env"
    global_env_file = tmp_path / "home" / ".env"
    global_env_file.parent.mkdir(parents=True)
    global_env_file.write_text("GH_TOKEN=global-token\n")
    before = global_env_file.read_text()

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="local",
            layout=InitWizardLayoutFacts(
                pycastle_dir=pycastle_dir,
                pycastle_home=tmp_path / "home",
                target_config_file=pycastle_dir / "config.py",
                target_env_file=local_env_file,
                local_env_file=local_env_file,
                global_env_file=global_env_file,
            ),
        )
    )

    assert plan.planned_env_file.should_manage is False
    assert plan.planned_env_file.should_create_local_env is True
    assert global_env_file.read_text() == before


def test_build_init_plan_uses_local_env_without_cross_scope_action_when_both_env_files_exist(
    tmp_path,
):
    from pycastle.init_wizard import (
        InitWizardLayoutFacts,
        InitWizardPlanningInputs,
        build_init_plan,
    )

    pycastle_dir = tmp_path / "pycastle"
    local_env_file = pycastle_dir / ".env"
    global_env_file = tmp_path / "home" / ".env"
    local_env_file.parent.mkdir(parents=True)
    global_env_file.parent.mkdir(parents=True)
    local_env_file.write_text("GH_TOKEN=local-token\n")
    global_env_file.write_text("GH_TOKEN=global-token\n")

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="local",
            layout=InitWizardLayoutFacts(
                pycastle_dir=pycastle_dir,
                pycastle_home=tmp_path / "home",
                target_config_file=pycastle_dir / "config.py",
                target_env_file=local_env_file,
                local_env_file=local_env_file,
                global_env_file=global_env_file,
            ),
            existing_env_keys=("GH_TOKEN",),
            existing_env_values={"GH_TOKEN": "local-token"},
        )
    )

    assert plan.planned_env_file.path == local_env_file
    assert plan.planned_env_file.should_manage is True
    assert plan.planned_env_file.should_create_local_env is False
    assert plan.planned_env_file.should_delete_local_env is False


def test_build_init_plan_accepts_explicit_env_existence_facts_without_reading_paths():
    from pycastle.init_wizard import InitWizardPlanningInputs, build_init_plan

    plan = build_init_plan(
        InitWizardPlanningInputs(
            selected_services=("claude",),
            scope_choice="local",
            layout=_layout_for_scope("local"),
            target_env_exists=False,
            local_env_exists=False,
            global_env_exists=True,
        )
    )

    assert plan.planned_env_file.should_manage is False
    assert plan.planned_env_file.should_create_local_env is True


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
