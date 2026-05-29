from __future__ import annotations

import dataclasses
import json
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..agents.output_protocol import AgentRole
from .resume import RoleSession, RunKind

if TYPE_CHECKING:
    from ..services.agent_service import AgentService


class AuthSeedingRequirement(Enum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"


class RecoveredSessionIdPersistence(Enum):
    PERSIST = "persist"
    SKIP = "skip"


def _codex_thread_id_from_rollouts(state_dir: Path) -> str | None:
    sessions_dir = state_dir / "sessions"
    if not sessions_dir.is_dir():
        return None
    found: set[str] = set()
    for rollout in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            lines = rollout.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "thread.started":
                continue
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                found.add(thread_id.strip())
    return next(iter(found)) if len(found) == 1 else None


def _codex_auth_seeding_requirement(
    state_dir: Path | None, run_kind: RunKind
) -> AuthSeedingRequirement:
    if state_dir is None:
        return AuthSeedingRequirement.NOT_REQUIRED
    if run_kind is RunKind.FRESH or not (state_dir / "auth.json").exists():
        return AuthSeedingRequirement.REQUIRED
    return AuthSeedingRequirement.NOT_REQUIRED


def _is_exact_resumable_provider_session(
    service_name: str,
    provider_session_id: str | None,
    state_dir: Path | None,
) -> bool:
    if provider_session_id is None or state_dir is None:
        return False
    if service_name == "codex":
        return _codex_thread_id_from_rollouts(state_dir) == provider_session_id
    return True


@dataclasses.dataclass(frozen=True)
class RunSessionPlan:
    role: AgentRole
    worktree: Path
    namespace: str
    service: AgentService
    run_kind: RunKind
    service_state_dir: Path | None
    provider_session_id: str | None
    auth_seeding_requirement: AuthSeedingRequirement
    recovered_session_id_persistence: RecoveredSessionIdPersistence
    exact_transcript_match: bool = False

    @classmethod
    def for_service(
        cls,
        *,
        role: AgentRole,
        worktree: Path,
        namespace: str,
        service: AgentService,
    ) -> RunSessionPlan:
        provider_session_id: str | None = None
        auth_seeding_requirement = AuthSeedingRequirement.NOT_REQUIRED
        recovered_session_id_persistence = RecoveredSessionIdPersistence.SKIP
        state_dir_relpath = service.state_dir_relpath(role, namespace)
        service_state_dir = (
            worktree / state_dir_relpath if state_dir_relpath is not None else None
        )
        run_kind = (
            RunKind.RESUME
            if service_state_dir is not None and service.is_resumable(service_state_dir)
            else RunKind.FRESH
        )
        role_session = RoleSession(worktree, role, namespace)
        if provider_session_id is None and service.name == "claude":
            provider_session_id = role_session.session_uuid()
        if provider_session_id is None and service.name == "codex":
            provider_session_id = role_session.service_session_id("codex")
            if provider_session_id is None and run_kind == RunKind.RESUME:
                if service_state_dir is not None:
                    provider_session_id = _codex_thread_id_from_rollouts(
                        service_state_dir
                    )
                if provider_session_id is not None:
                    recovered_session_id_persistence = (
                        RecoveredSessionIdPersistence.PERSIST
                    )
                if provider_session_id is None:
                    run_kind = RunKind.FRESH
        if provider_session_id is None and service.name == "opencode":
            provider_session_id = role_session.service_session_id("opencode")
            if provider_session_id is None and run_kind is RunKind.RESUME:
                run_kind = RunKind.FRESH
        if service.name == "codex":
            auth_seeding_requirement = _codex_auth_seeding_requirement(
                service_state_dir, run_kind
            )
        metadata = role_session.service_session_metadata(service.name)
        exact_transcript_match = (
            run_kind is RunKind.RESUME
            and metadata is not None
            and metadata["provider_session_id"] == provider_session_id
            and _is_exact_resumable_provider_session(
                service.name, provider_session_id, service_state_dir
            )
        )
        return cls(
            role=role,
            worktree=worktree,
            namespace=namespace,
            service=service,
            run_kind=run_kind,
            service_state_dir=service_state_dir,
            provider_session_id=provider_session_id,
            auth_seeding_requirement=auth_seeding_requirement,
            recovered_session_id_persistence=recovered_session_id_persistence,
            exact_transcript_match=exact_transcript_match,
        )


__all__ = [
    "AuthSeedingRequirement",
    "RecoveredSessionIdPersistence",
    "RunSessionPlan",
]
