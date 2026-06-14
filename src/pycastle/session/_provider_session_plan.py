from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pycastle_agent_runtime.provider_session_adapter import (
    ProviderSessionAdapter,
    ProviderSessionPlanningFacts,
    ProviderSessionPlanningRequest,
)
from pycastle_agent_runtime.session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
    provider_state_relpath,
)
from pycastle_agent_runtime.session_planning import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    ProviderRunStatePlan,
    ProviderRunStatePlanRequest as RuntimeProviderRunStatePlanRequest,
    ProviderSessionDecision,
    RecoveredSessionIdPersistence,
    plan_provider_run_state as runtime_plan_provider_run_state,
    plan_provider_session as runtime_plan_provider_session,
    record_observed_provider_session_id as runtime_record_observed_provider_session_id,
    record_successful_provider_session_metadata as runtime_record_successful_provider_session_metadata,
)

from ..agents.output_protocol import AgentRole
from .resume import RoleSession

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


class _BaseProviderSessionAdapter:
    def __init__(self, service_name: str) -> None:
        self._service_name = service_name

    @property
    def service_name(self) -> str:
        return self._service_name

    def provider_session_planning_facts(
        self,
        request: ProviderSessionPlanningRequest,
    ) -> ProviderSessionPlanningFacts:
        state_dir_relpath = provider_state_relpath(
            request.role,
            self.service_name,
            request.namespace,
            session_root=".pycastle-session",
        )
        provider_state_dir = request.worktree / state_dir_relpath.rstrip("/")
        return ProviderSessionPlanningFacts(
            state_dir_relpath=state_dir_relpath,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=self._has_resumable_provider_state(
                provider_state_dir
            ),
        )

    def prepare_local_provider_run_state(
        self,
        provider_state_dir: Path | None,
        auth_seed_action: LocalAuthSeedAction | None = None,
    ) -> None:
        if provider_state_dir is not None:
            provider_state_dir.mkdir(parents=True, exist_ok=True)
        if auth_seed_action is not None:
            auth_seed_action.apply()

    def record_provider_session_id(
        self,
        *,
        role_session: RoleSession,
        provider_session_id: str,
        service_state_dir: Path | None = None,
    ) -> None:
        del role_session, provider_session_id, service_state_dir

    def recover_provider_session_id(
        self,
        provider_state_dir: Path | None,
    ) -> str | None:
        del provider_state_dir
        return None

    def is_exact_resumable_provider_session(
        self,
        *,
        provider_session_id: str | None,
        provider_state_dir: Path | None,
    ) -> bool:
        return provider_session_id is not None and provider_state_dir is not None

    def _has_resumable_provider_state(self, provider_state_dir: Path) -> bool:
        return provider_state_dir.is_dir() and any(
            candidate.is_file() for candidate in provider_state_dir.rglob("*")
        )


class _ClaudeProviderSessionAdapter(_BaseProviderSessionAdapter):
    def __init__(self) -> None:
        super().__init__("claude")

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        from ..services.claude_service import _provider_session_preferences_for_request

        return _provider_session_preferences_for_request(request)

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        from ..services.claude_service import _provider_session_state_for_request

        return _provider_session_state_for_request(request)


class _DelegatingProviderSessionAdapter(_BaseProviderSessionAdapter):
    def __init__(self, service_name: str, service: AgentService | None = None) -> None:
        super().__init__(service_name)
        self._service = service

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        if self._service is None:
            raise RuntimeError(
                "provider session selection requires a concrete provider session service"
            )
        return self._service.provider_session_preferences(request)

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        if self._service is None:
            raise RuntimeError(
                "provider session selection requires a concrete provider session service"
            )
        return self._service.provider_session_state(request)

    def provider_session_planning_facts(
        self,
        request: ProviderSessionPlanningRequest,
    ) -> ProviderSessionPlanningFacts:
        if self._service is None:
            raise RuntimeError(
                "provider session selection requires a concrete provider session service"
            )
        state_dir_relpath = self._service.state_dir_relpath(
            request.role,
            request.namespace,
        )
        provider_state_dir = (
            None
            if state_dir_relpath is None
            else request.worktree / state_dir_relpath.rstrip("/")
        )
        has_resumable_provider_state = (
            provider_state_dir is not None
            and self._service.is_resumable(provider_state_dir)
        )
        return ProviderSessionPlanningFacts(
            state_dir_relpath=state_dir_relpath,
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=has_resumable_provider_state,
        )


class _CodexProviderSessionAdapter(_DelegatingProviderSessionAdapter):
    def __init__(self, service: AgentService | None = None) -> None:
        super().__init__("codex", service)

    def record_provider_session_id(
        self,
        *,
        role_session: RoleSession,
        provider_session_id: str,
        service_state_dir: Path | None = None,
    ) -> None:
        del service_state_dir
        role_session.save_service_session_id(self.service_name, provider_session_id)

    def recover_provider_session_id(
        self,
        provider_state_dir: Path | None,
    ) -> str | None:
        return _recover_codex_rollout_thread_id(provider_state_dir)

    def is_exact_resumable_provider_session(
        self,
        *,
        provider_session_id: str | None,
        provider_state_dir: Path | None,
    ) -> bool:
        return (
            self.recover_provider_session_id(provider_state_dir) == provider_session_id
        )


class _OpenCodeProviderSessionAdapter(_DelegatingProviderSessionAdapter):
    def __init__(self, service: AgentService | None = None) -> None:
        super().__init__("opencode", service)

    def record_provider_session_id(
        self,
        *,
        role_session: RoleSession,
        provider_session_id: str,
        service_state_dir: Path | None = None,
    ) -> None:
        role_session.save_service_session_id(self.service_name, provider_session_id)
        if service_state_dir is None:
            return
        session_id_path = service_state_dir / "session_id"
        session_id_path.parent.mkdir(parents=True, exist_ok=True)
        session_id_path.write_text(provider_session_id, encoding="utf-8")


def _provider_session_adapter(service: "AgentService") -> ProviderSessionAdapter | None:
    from ..services.claude_service import ClaudeService

    if isinstance(service, ClaudeService):
        return cast(ProviderSessionAdapter, _ClaudeProviderSessionAdapter())
    if service.name == "codex":
        return cast(ProviderSessionAdapter, _CodexProviderSessionAdapter(service))
    if service.name == "opencode":
        return cast(ProviderSessionAdapter, _OpenCodeProviderSessionAdapter(service))
    return None


def provider_session_adapter_for_service_name(
    service_name: str,
) -> ProviderSessionAdapter | None:
    if service_name == "claude":
        return cast(ProviderSessionAdapter, _ClaudeProviderSessionAdapter())
    if service_name == "codex":
        return cast(ProviderSessionAdapter, _CodexProviderSessionAdapter())
    if service_name == "opencode":
        return cast(ProviderSessionAdapter, _OpenCodeProviderSessionAdapter())
    return None


def _recover_codex_rollout_thread_id(state_dir: Path | None) -> str | None:
    if state_dir is None:
        return None
    sessions_dir = state_dir / "sessions"
    if not sessions_dir.is_dir():
        return None

    found: set[str] = set()
    for rollout in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            lines = rollout.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "thread.started":
                continue
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                found.add(thread_id.strip())

    return next(iter(found)) if len(found) == 1 else None


@dataclasses.dataclass(frozen=True)
class ProviderRunStatePlanRequest:
    worktree: Path
    role: AgentRole
    namespace: str
    service: AgentService


ProviderSessionPlanRequest = ProviderRunStatePlanRequest


def _role_session(
    worktree: Path,
    role: AgentRole,
    namespace: str,
) -> RoleSession:
    return RoleSession(worktree, role, namespace)


def _metadata_plan(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str | None,
) -> ProviderRunStatePlan:
    role_session = _role_session(worktree, role, namespace)
    return ProviderRunStatePlan(
        role_session=role_session,
        provider_session_adapter=provider_session_adapter_for_service_name(
            service_name
        ),
        service_name=service_name,
        run_kind=role_session.run_kind(),
        provider_state_dir=service_state_dir,
        provider_state_dir_relpath=None,
        provider_session_id=provider_session_id,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
        service_state_dir=service_state_dir,
    )


def _runtime_request(
    request: ProviderRunStatePlanRequest,
) -> RuntimeProviderRunStatePlanRequest:
    return RuntimeProviderRunStatePlanRequest(
        worktree=request.worktree,
        role=request.role,
        namespace=request.namespace,
        service=request.service,
        role_session=_role_session(request.worktree, request.role, request.namespace),
        provider_session_adapter=_provider_session_adapter(request.service),
    )


def record_observed_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    runtime_record_observed_provider_session_id(
        provider_run_state_plan=_metadata_plan(
            worktree=worktree,
            role=role,
            namespace=namespace,
            service_name=service_name,
            service_state_dir=service_state_dir,
            provider_session_id=None,
        ),
        provider_session_id=provider_session_id,
    )


def capture_provider_session_id(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    service_state_dir: Path | None,
    provider_session_id: str,
) -> None:
    record_observed_provider_session_id(
        worktree=worktree,
        role=role,
        namespace=namespace,
        service_name=service_name,
        service_state_dir=service_state_dir,
        provider_session_id=provider_session_id,
    )


def record_successful_provider_session_metadata(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service_name: str,
    provider_session_id: str | None,
) -> None:
    runtime_record_successful_provider_session_metadata(
        provider_run_state_plan=_metadata_plan(
            worktree=worktree,
            role=role,
            namespace=namespace,
            service_name=service_name,
            service_state_dir=None,
            provider_session_id=provider_session_id,
        ),
        provider_session_id=provider_session_id,
    )


def plan_provider_session(
    request: ProviderRunStatePlanRequest,
) -> ProviderSessionDecision:
    return runtime_plan_provider_session(_runtime_request(request))


def plan_provider_run_state(
    request: ProviderRunStatePlanRequest,
) -> ProviderRunStatePlan:
    return runtime_plan_provider_run_state(_runtime_request(request))


__all__ = [
    "AuthSeedingRequirement",
    "capture_provider_session_id",
    "LocalAuthSeedAction",
    "ProviderRunStatePlan",
    "ProviderRunStatePlanRequest",
    "ProviderSessionDecision",
    "ProviderSessionPlanRequest",
    "plan_provider_run_state",
    "plan_provider_session",
    "record_observed_provider_session_id",
    "record_successful_provider_session_metadata",
]
