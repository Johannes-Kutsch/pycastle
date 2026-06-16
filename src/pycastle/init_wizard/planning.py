from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

InitWizardScopeChoice = Literal["global", "local"]
EnvKeyActionKind = Literal["keep", "prompt", "overwrite_prompt", "add_missing"]
PlannedEnvFileAction = Literal["create", "merge", "preserve"]

_SUPPORTED_SERVICE_SELECTIONS: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex",),
    "opencode": ("opencode",),
    "all": ("claude", "codex", "opencode"),
}
_GITHUB_KEY = "GH_TOKEN"
_CLAUDE_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
_OPENCODE_KEY = "OPENCODE_GO_API_KEY"
_CODEX_HOST_AUTH_WARNING = (
    "Codex authentication missing: run `codex login` on the host."
)
_BUNDLED_STAGE_COVERAGE_WARNING = (
    "selected services do not cover every bundled default stage priority chain. "
    "Define your own stage overrides in config.py before running pycastle."
)
_PROMPT_TEXT_BY_KEY = {
    _GITHUB_KEY: "GitHub token (press Enter to skip)",
    _CLAUDE_KEY: (
        "Claude OAuth token (run `claude setup-token` to generate one; "
        "press Enter to skip)"
    ),
    _OPENCODE_KEY: "OpenCode Go API key (press Enter to skip)",
}


@dataclass(frozen=True)
class InitWizardLayoutFacts:
    pycastle_dir: Path
    pycastle_home: Path
    target_config_file: Path
    target_env_file: Path
    local_env_file: Path
    global_env_file: Path


@dataclass(frozen=True)
class HostAuthFacts:
    has_host_codex_auth: bool


@dataclass(frozen=True)
class ScaffoldStageChainFacts:
    bundled_default_stage_chains: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class PlannedEnvFile:
    path: Path
    should_manage: bool
    action: PlannedEnvFileAction = "create"
    should_delete_local_env: bool = False
    should_create_local_env: bool = False
    missing_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvKeyAction:
    key: str
    action: EnvKeyActionKind


@dataclass(frozen=True)
class CredentialPrompt:
    key: str
    prompt_text: str
    allow_overwrite: bool


@dataclass(frozen=True)
class PlannedWarning:
    message: str


@dataclass(frozen=True)
class ConfigHintAction:
    key: str
    value: str


@dataclass(frozen=True)
class ConfigFileAction:
    path: Path
    should_create: bool
    hints: tuple[ConfigHintAction, ...] = ()


@dataclass(frozen=True)
class LabelPromptEligibility:
    should_prompt: bool
    reason: str | None = None


@dataclass(frozen=True)
class InitWizardPlanningInputs:
    selected_services: tuple[str, ...]
    scope_choice: InitWizardScopeChoice
    layout: InitWizardLayoutFacts
    existing_env_keys: tuple[str, ...] = ()
    existing_env_values: dict[str, str] = field(default_factory=dict)
    target_env_exists: bool | None = None
    local_env_exists: bool | None = None
    global_env_exists: bool | None = None
    host_auth: HostAuthFacts = field(default_factory=lambda: HostAuthFacts(False))
    scaffold_stage_chains: ScaffoldStageChainFacts = field(
        default_factory=ScaffoldStageChainFacts
    )


@dataclass(frozen=True)
class InitPlan:
    selected_services: tuple[str, ...]
    scope_choice: InitWizardScopeChoice
    target_config_file: Path
    planned_env_file: PlannedEnvFile
    env_key_actions: tuple[EnvKeyAction, ...] = ()
    credential_prompts: tuple[CredentialPrompt, ...] = ()
    warnings: tuple[PlannedWarning, ...] = ()
    config_file_action: ConfigFileAction | None = None
    label_prompt_eligibility: LabelPromptEligibility = field(
        default_factory=lambda: LabelPromptEligibility(False)
    )


def _normalize_selected_services(selected_services: tuple[str, ...]) -> tuple[str, ...]:
    if not selected_services:
        return _SUPPORTED_SERVICE_SELECTIONS["all"]

    normalized_services = tuple(
        service.strip().lower() for service in selected_services
    )

    if len(selected_services) == 1:
        selection = normalized_services[0] or "all"
        service_set = _SUPPORTED_SERVICE_SELECTIONS.get(selection)
        if service_set is not None:
            return service_set
        choices = "/".join(_SUPPORTED_SERVICE_SELECTIONS)
        raise ValueError(
            f"Invalid service selection {selected_services[0]!r}. Choose one of: {choices}."
        )

    supported_services = set(_SUPPORTED_SERVICE_SELECTIONS) - {"all"}
    invalid_index = next(
        (
            index
            for index, service in enumerate(normalized_services)
            if service not in supported_services
        ),
        None,
    )
    if invalid_index is not None:
        choices = "/".join(_SUPPORTED_SERVICE_SELECTIONS)
        raise ValueError(
            "Invalid service selection "
            f"{selected_services[invalid_index]!r}. Choose one of: {choices}."
        )

    return normalized_services


def _managed_env_keys(selected_services: tuple[str, ...]) -> tuple[str, ...]:
    keys = [_GITHUB_KEY, _CLAUDE_KEY]
    if "opencode" in selected_services:
        keys.append(_OPENCODE_KEY)
    return tuple(keys)


def _prompted_env_keys(selected_services: tuple[str, ...]) -> tuple[str, ...]:
    keys = [_GITHUB_KEY]
    if "claude" in selected_services:
        keys.append(_CLAUDE_KEY)
    if "opencode" in selected_services:
        keys.append(_OPENCODE_KEY)
    return tuple(keys)


def _resolve_env_exists(explicit_fact: bool | None, path: Path) -> bool:
    if explicit_fact is not None:
        return explicit_fact
    return path.exists()


def _covers_bundled_default_stage_chains(
    selected_services: tuple[str, ...],
    scaffold_stage_chains: ScaffoldStageChainFacts,
) -> bool:
    selected = set(selected_services)
    return all(
        any(service in selected for service in stage)
        for stage in scaffold_stage_chains.bundled_default_stage_chains
    )


def _plan_warnings(inputs: InitWizardPlanningInputs) -> tuple[PlannedWarning, ...]:
    selected_services = _normalize_selected_services(inputs.selected_services)
    warnings: list[PlannedWarning] = []

    if "codex" in selected_services and not inputs.host_auth.has_host_codex_auth:
        warnings.append(PlannedWarning(message=_CODEX_HOST_AUTH_WARNING))

    if not _covers_bundled_default_stage_chains(
        selected_services, inputs.scaffold_stage_chains
    ):
        warnings.append(PlannedWarning(message=_BUNDLED_STAGE_COVERAGE_WARNING))

    return tuple(warnings)


def build_init_plan(inputs: InitWizardPlanningInputs) -> InitPlan:
    selected_services = _normalize_selected_services(inputs.selected_services)
    managed_env_keys = _managed_env_keys(selected_services)
    prompted_env_keys = set(_prompted_env_keys(selected_services))
    existing_keys = set(inputs.existing_env_keys) | set(inputs.existing_env_values)
    existing_values = {
        key: value for key, value in inputs.existing_env_values.items() if value
    }
    target_env_exists = _resolve_env_exists(
        inputs.target_env_exists, inputs.layout.target_env_file
    )
    local_env_exists = _resolve_env_exists(
        inputs.local_env_exists, inputs.layout.local_env_file
    )
    global_env_exists = _resolve_env_exists(
        inputs.global_env_exists, inputs.layout.global_env_file
    )
    should_delete_local_env = inputs.scope_choice == "global" and local_env_exists
    should_create_local_env = (
        inputs.scope_choice == "local"
        and global_env_exists
        and not local_env_exists
        and not target_env_exists
    )
    should_manage = not should_create_local_env
    planned_missing_keys = tuple(
        key for key in managed_env_keys if key not in existing_keys
    )
    planned_env_action: PlannedEnvFileAction
    if not target_env_exists and not existing_keys:
        planned_env_action = "create"
    elif planned_missing_keys:
        planned_env_action = "merge"
    else:
        planned_env_action = "preserve"

    return InitPlan(
        selected_services=selected_services,
        scope_choice=inputs.scope_choice,
        target_config_file=inputs.layout.target_config_file,
        planned_env_file=PlannedEnvFile(
            path=inputs.layout.target_env_file,
            should_manage=should_manage,
            action=planned_env_action,
            should_delete_local_env=should_delete_local_env,
            should_create_local_env=should_create_local_env,
            missing_keys=planned_missing_keys,
        ),
        env_key_actions=tuple(
            EnvKeyAction(
                key=key,
                action=(
                    "overwrite_prompt"
                    if key in existing_values and key in prompted_env_keys
                    else "keep"
                    if key in existing_keys
                    else "prompt"
                    if key in prompted_env_keys
                    else "add_missing"
                ),
            )
            for key in managed_env_keys
        ),
        credential_prompts=tuple(
            CredentialPrompt(
                key=key,
                prompt_text=_PROMPT_TEXT_BY_KEY[key],
                allow_overwrite=key in existing_values,
            )
            for key in managed_env_keys
            if key in prompted_env_keys
        ),
        warnings=_plan_warnings(inputs),
    )
