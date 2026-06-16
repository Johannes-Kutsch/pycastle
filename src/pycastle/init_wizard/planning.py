from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

InitWizardScopeChoice = Literal["global", "local"]
EnvKeyActionKind = Literal["keep", "prompt", "overwrite_prompt", "add_missing"]


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
