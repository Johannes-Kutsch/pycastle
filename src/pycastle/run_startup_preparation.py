from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

import pycastle_agent_runtime as agent_runtime
from pycastle_agent_runtime.service_registry import ServiceRegistry

from .config import Config, KNOWN_CREDENTIAL_ENV_KEYS, StageOverride
from .config.loader import referenced_services

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .services.agent_service import AgentService

_KNOWN_SERVICES: frozenset[str] = frozenset({"claude", "codex", "opencode"})
RunImproveMode: TypeAlias = Literal["until_sleep", "endless"] | None


@dataclass(frozen=True)
class RunStartupImproveModeFlagFacts:
    no_improve: bool
    improve_mode_flag: RunImproveMode


@dataclass(frozen=True)
class RunStartupPreparation:
    validation_failures: tuple[str, ...]
    configured_provider_adapters: dict[str, "AgentService"]
    runtime_registry: ServiceRegistry
    shared_container_env: dict[str, str]
    effective_improve_mode: RunImproveMode


def prepare_run_startup(
    cfg: Config,
    credential_env: Mapping[str, str],
    improve_mode_flags: RunStartupImproveModeFlagFacts,
) -> RunStartupPreparation:
    configured_provider_adapters = configured_provider_adapters_for_run(
        cfg, credential_env
    )
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
        validation_failures = tuple(
            _validate_locally_configured_stage_overrides(
                cfg, configured_provider_adapters
            )
        )
    return RunStartupPreparation(
        validation_failures=tuple(validation_failures),
        configured_provider_adapters=configured_provider_adapters,
        runtime_registry=ServiceRegistry(configured_provider_adapters),
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

    primary = credential_env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not primary:
        return service_registry

    accounts: list[tuple[str, str]] = []
    secondary = credential_env.get("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY")
    if secondary:
        accounts.append(("secondary", secondary))
    accounts.append(("primary", primary))
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
) -> list[str]:
    if valid_models_by_service is None:
        valid_models_by_service = {}
    violations: list[str] = []
    for stage_name, override in _stage_overrides(cfg):
        for stage_label, entry in zip(
            agent_runtime.validation_labels(stage_name, override),
            agent_runtime.chain_entries(override),
            strict=True,
        ):
            svc_name = entry.service
            valid_efforts: frozenset[str] | None = None
            if not svc_name:
                violations.append(f"  stage={stage_label!r}: service is required")
            else:
                valid_efforts = valid_efforts_by_service.get(svc_name)
                if valid_efforts is None:
                    violations.append(
                        f"  stage={stage_label!r}: service={svc_name!r} is not a known service"
                        f" (known: {sorted(_KNOWN_SERVICES)})"
                    )
            if not entry.effort:
                violations.append(f"  stage={stage_label!r}: effort is required")
            elif valid_efforts is not None and entry.effort not in valid_efforts:
                violations.append(
                    f"  stage={stage_label!r}: effort={entry.effort!r} is invalid"
                    f" for service={svc_name!r} (valid: {sorted(valid_efforts)})"
                )
            if svc_name and entry.model:
                valid_models = valid_models_by_service.get(svc_name)
                if valid_models is not None and entry.model not in valid_models:
                    suggestion = difflib.get_close_matches(
                        entry.model, sorted(valid_models), n=1
                    )
                    detail = (
                        f' Did you mean "{suggestion[0]}"?'
                        if suggestion
                        else f" (valid: {sorted(valid_models)})"
                    )
                    violations.append(
                        f"  stage={stage_label!r}: model={entry.model!r} is invalid"
                        f" for service={svc_name!r}.{detail}"
                    )
    return violations


def _validate_locally_configured_stage_overrides(
    cfg: Config, configured_provider_adapters: dict[str, "AgentService"]
) -> list[str]:
    registry = ServiceRegistry(configured_provider_adapters)
    violations: list[str] = []
    for stage_name, override in _stage_overrides(cfg):
        if registry.has_configured_candidate(override):
            continue
        violations.append(
            f"  stage={stage_name!r}: no locally configured service in priority chain "
            f"{agent_runtime.render_chain_label(override)!r}"
        )
    return violations


def _shared_container_env(credential_env: Mapping[str, str]) -> dict[str, str]:
    # Strip provider-specific credentials from the shared env; service adapters
    # inject what they need into the agent container at the streaming boundary.
    return {
        key: value
        for key, value in credential_env.items()
        if key in KNOWN_CREDENTIAL_ENV_KEYS
        and key not in {"CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "OPENCODE_GO_API_KEY"}
    }


def _effective_improve_mode(
    cfg: Config, improve_mode_flags: RunStartupImproveModeFlagFacts
) -> RunImproveMode:
    if improve_mode_flags.no_improve:
        return None
    if improve_mode_flags.improve_mode_flag is not None:
        return improve_mode_flags.improve_mode_flag
    return cfg.improve_mode
