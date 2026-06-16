from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

InitWizardScopeChoice = Literal["global", "local"]
EnvKeyActionKind = Literal["keep", "prompt", "overwrite_prompt", "add_missing"]

_SUPPORTED_SERVICE_SELECTIONS: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex",),
    "opencode": ("opencode",),
    "all": ("claude", "codex", "opencode"),
}
_GITHUB_KEY = "GH_TOKEN"
_CLAUDE_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
_OPENCODE_KEY = "OPENCODE_GO_API_KEY"
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
    should_delete_local_env: bool = False
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
    existing_env_values: dict[str, str] = field(default_factory=dict)
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

    if len(selected_services) == 1:
        selection = selected_services[0]
        service_set = _SUPPORTED_SERVICE_SELECTIONS.get(selection.lower())
        if service_set is not None:
            return service_set
        choices = "/".join(_SUPPORTED_SERVICE_SELECTIONS)
        raise ValueError(
            f"Invalid service selection {selection!r}. Choose one of: {choices}."
        )

    return tuple(service.lower() for service in selected_services)


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


def build_init_plan(inputs: InitWizardPlanningInputs) -> InitPlan:
    selected_services = _normalize_selected_services(inputs.selected_services)
    managed_env_keys = _managed_env_keys(selected_services)
    prompted_env_keys = set(_prompted_env_keys(selected_services))
    existing_values = {
        key: value for key, value in inputs.existing_env_values.items() if value
    }

    return InitPlan(
        selected_services=selected_services,
        scope_choice=inputs.scope_choice,
        target_config_file=inputs.layout.target_config_file,
        planned_env_file=PlannedEnvFile(
            path=inputs.layout.target_env_file,
            should_manage=True,
            missing_keys=tuple(
                key for key in managed_env_keys if key not in existing_values
            ),
        ),
        env_key_actions=tuple(
            EnvKeyAction(
                key=key,
                action=(
                    "overwrite_prompt"
                    if key in existing_values and key in prompted_env_keys
                    else "keep"
                    if key in existing_values
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
    )
