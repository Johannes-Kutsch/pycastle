from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from .services.service_registry import ServiceRegistry

from .config import Config, StageOverride, parse_credential_list
from .config.loader import referenced_services
from .stage_priority_chain import (
    chain_entries,
    render_chain_label,
    validation_labels,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .services.agent_service import AgentService

_KNOWN_SERVICES: frozenset[str] = frozenset({"claude", "codex", "opencode"})
RunImproveMode: TypeAlias = Literal["until_sleep", "endless"] | None


@dataclass(frozen=True)
class StageOverrideValidationFailure:
    code: Literal[
        "missing_service",
        "missing_effort",
        "unknown_service",
        "invalid_effort",
        "invalid_model",
        "provider_model_mismatch",
        "no_configured_service",
        "credential_pool_error",
    ]
    stage_label: str
    service: str | None = None
    effort: str | None = None
    model: str | None = None
    known_services: tuple[str, ...] = ()
    valid_values: tuple[str, ...] = ()
    suggestion: str | None = None
    chain_label: str | None = None
    message: str | None = None

    def render(self) -> str:
        if self.code == "missing_service":
            return f"  stage={self.stage_label!r}: service is required"
        if self.code == "missing_effort":
            return f"  stage={self.stage_label!r}: effort is required"
        if self.code == "unknown_service":
            return (
                f"  stage={self.stage_label!r}: service={self.service!r} is not a known service"
                f" (known: {list(self.known_services)!r})"
            )
        if self.code == "invalid_effort":
            return (
                f"  stage={self.stage_label!r}: effort={self.effort!r} is invalid"
                f" for service={self.service!r} (valid: {list(self.valid_values)!r})"
            )
        if self.code in {"invalid_model", "provider_model_mismatch"}:
            detail = (
                f' Did you mean "{self.suggestion}"?'
                if self.suggestion
                else f" (valid: {list(self.valid_values)!r})"
            )
            return (
                f"  stage={self.stage_label!r}: model={self.model!r} is invalid"
                f" for service={self.service!r}.{detail}"
            )
        if self.code == "credential_pool_error" and self.message is not None:
            return f"  {self.message}"
        return (
            f"  stage={self.stage_label!r}: no locally configured service in priority chain "
            f"{self.chain_label!r}"
        )


@dataclass(frozen=True)
class RunStartupImproveModeFlagFacts:
    no_improve: bool
    improve_mode_flag: RunImproveMode


@dataclass(frozen=True)
class RunStartupPreparation:
    validation_failures: tuple[StageOverrideValidationFailure, ...]
    configured_provider_adapters: dict[str, "AgentService"]
    runtime_registry: ServiceRegistry
    shared_container_env: dict[str, str]
    effective_improve_mode: RunImproveMode

    @property
    def validation_error_message(self) -> str | None:
        if not self.validation_failures:
            return None
        return "Config validation errors:\n" + "\n".join(
            failure.render() for failure in self.validation_failures
        )


def prepare_run_startup(
    cfg: Config,
    credential_env: Mapping[str, str],
    improve_mode_flags: RunStartupImproveModeFlagFacts,
) -> RunStartupPreparation:
    configuration_failures: tuple[StageOverrideValidationFailure, ...] = ()
    try:
        configured_provider_adapters = configured_provider_adapters_for_run(
            cfg,
            credential_env,
        )
    except ValueError as err:
        configured_provider_adapters = {}
        configuration_failures = (
            StageOverrideValidationFailure(
                code="credential_pool_error",
                stage_label="startup",
                message=str(err),
            ),
        )
    runtime_registry = ServiceRegistry(configured_provider_adapters)
    validation_services = _validation_services()
    validation_failures = tuple(
        _validate_stage_overrides(
            cfg,
            {
                name: service.valid_efforts()
                for name, service in validation_services.items()
            },
            {
                name: service.valid_models()
                for name, service in validation_services.items()
            },
        )
    )
    if not validation_failures:
        validation_failures = (
            *tuple(
                _validate_configured_provider_stage_overrides(
                    cfg, configured_provider_adapters
                )
            ),
            *tuple(_validate_locally_configured_stage_overrides(cfg, runtime_registry)),
        )
    validation_failures = (*configuration_failures, *validation_failures)
    return RunStartupPreparation(
        validation_failures=tuple(validation_failures),
        configured_provider_adapters=configured_provider_adapters,
        runtime_registry=runtime_registry,
        shared_container_env=_shared_container_env(credential_env),
        effective_improve_mode=_effective_improve_mode(cfg, improve_mode_flags),
    )


def configured_provider_adapters_for_run(
    cfg: Config, credential_env: Mapping[str, str]
) -> dict[str, "AgentService"]:
    from .services.claude_service import ClaudeService
    from .services.codex_service import CodexService
    from .services.opencode_service import OpenCodeService

    referenced = referenced_services(cfg)
    service_registry: dict[str, AgentService] = {}

    if "codex" in referenced:
        service_registry["codex"] = CodexService()

    if "opencode" in referenced and credential_env.get("OPENCODE_GO_API_KEY"):
        service_registry["opencode"] = OpenCodeService(
            api_key=credential_env.get("OPENCODE_GO_API_KEY")
        )

    if "claude" not in referenced:
        return service_registry

    accounts_with_slots = parse_credential_list(
        credential_env,
        "CLAUDE_CODE_OAUTH_TOKEN",
    )

    if not accounts_with_slots:
        return service_registry

    accounts = [(f"account {slot}", token) for slot, token in accounts_with_slots]
    service_registry["claude"] = ClaudeService(accounts=accounts)
    return service_registry


def _validation_services() -> dict[str, "AgentService"]:
    from .services.claude_service import ClaudeService
    from .services.codex_service import CodexService
    from .services.opencode_service import OpenCodeService

    return {
        "claude": ClaudeService(),
        "codex": CodexService(),
        "opencode": OpenCodeService(),
    }


def _stage_overrides(cfg: Config) -> list[tuple[str, StageOverride]]:
    return [
        ("plan", cfg.plan_override),
        ("implement", cfg.implement_override),
        ("review", cfg.review_override),
        ("merge", cfg.merge_override),
        ("preflight_issue", cfg.preflight_issue_override),
        ("improve", cfg.improve_override),
    ]


def _validate_stage_overrides(
    cfg: Config,
    valid_efforts_by_service: dict[str, frozenset[str]],
    valid_models_by_service: dict[str, frozenset[str]] | None = None,
) -> list[StageOverrideValidationFailure]:
    if valid_models_by_service is None:
        valid_models_by_service = {}
    violations: list[StageOverrideValidationFailure] = []
    for stage_name, override in _stage_overrides(cfg):
        for stage_label, entry in zip(
            validation_labels(stage_name, override),
            chain_entries(override),
            strict=True,
        ):
            svc_name = entry.service
            valid_efforts: frozenset[str] | None = None
            if not svc_name:
                violations.append(
                    StageOverrideValidationFailure(
                        code="missing_service",
                        stage_label=stage_label,
                    )
                )
            else:
                valid_efforts = valid_efforts_by_service.get(svc_name)
                if valid_efforts is None:
                    violations.append(
                        StageOverrideValidationFailure(
                            code="unknown_service",
                            stage_label=stage_label,
                            service=svc_name,
                            known_services=tuple(sorted(_KNOWN_SERVICES)),
                        )
                    )
            if not entry.effort:
                violations.append(
                    StageOverrideValidationFailure(
                        code="missing_effort",
                        stage_label=stage_label,
                    )
                )
            elif valid_efforts is not None and entry.effort not in valid_efforts:
                violations.append(
                    StageOverrideValidationFailure(
                        code="invalid_effort",
                        stage_label=stage_label,
                        service=svc_name,
                        effort=entry.effort,
                        valid_values=tuple(sorted(valid_efforts)),
                    )
                )
            if svc_name and entry.model:
                valid_models = valid_models_by_service.get(svc_name)
                if valid_models is not None and entry.model not in valid_models:
                    suggestion = difflib.get_close_matches(
                        entry.model, sorted(valid_models), n=1
                    )
                    violations.append(
                        StageOverrideValidationFailure(
                            code="invalid_model",
                            stage_label=stage_label,
                            service=svc_name,
                            model=entry.model,
                            valid_values=tuple(sorted(valid_models)),
                            suggestion=suggestion[0] if suggestion else None,
                        )
                    )
    return violations


def _validate_locally_configured_stage_overrides(
    cfg: Config, runtime_registry: ServiceRegistry
) -> list[StageOverrideValidationFailure]:
    violations: list[StageOverrideValidationFailure] = []
    for stage_name, override in _stage_overrides(cfg):
        if runtime_registry.has_configured_candidate(override):
            continue
        violations.append(
            StageOverrideValidationFailure(
                code="no_configured_service",
                stage_label=stage_name,
                chain_label=render_chain_label(override),
            )
        )
    return violations


def _validate_configured_provider_stage_overrides(
    cfg: Config, configured_provider_adapters: Mapping[str, "AgentService"]
) -> list[StageOverrideValidationFailure]:
    violations: list[StageOverrideValidationFailure] = []
    for stage_name, override in _stage_overrides(cfg):
        for stage_label, entry in zip(
            validation_labels(stage_name, override),
            chain_entries(override),
            strict=True,
        ):
            if not entry.model:
                continue
            service = configured_provider_adapters.get(entry.service)
            if service is None:
                continue
            valid_models = tuple(sorted(service.valid_models()))
            if entry.model in valid_models:
                continue
            suggestion = difflib.get_close_matches(entry.model, valid_models, n=1)
            violations.append(
                StageOverrideValidationFailure(
                    code="provider_model_mismatch",
                    stage_label=stage_label,
                    service=entry.service,
                    model=entry.model,
                    valid_values=valid_models,
                    suggestion=suggestion[0] if suggestion else None,
                )
            )
    return violations


def _shared_container_env(credential_env: Mapping[str, str]) -> dict[str, str]:
    # Strip provider-specific credentials from the shared env; service adapters
    # inject what they need into the agent container at the streaming boundary.
    return {
        key: value
        for key, value in credential_env.items()
        if key
        not in {
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
            "OPENCODE_GO_API_KEY",
        }
    }


def _effective_improve_mode(
    cfg: Config, improve_mode_flags: RunStartupImproveModeFlagFacts
) -> RunImproveMode:
    if improve_mode_flags.no_improve:
        return None
    if improve_mode_flags.improve_mode_flag is not None:
        return improve_mode_flags.improve_mode_flag
    return cfg.improve_mode
