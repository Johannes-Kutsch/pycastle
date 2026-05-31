from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.errors import HardAgentError
from pycastle.services import ClaudeService
from pycastle.services.codex_service import CodexService
from pycastle.services.opencode_service import OpenCodeService
from pycastle.services.provider_session_state import (
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.session.agent import RunSessionPlanRequest, plan_run_session
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RecoveredSessionIdPersistence,
    RunSessionPlan,
)
from pycastle.session import (
    ProviderIdentityKind,
    RoleSession,
    RunKind,
)
from pycastle.session.service_resume_identity import (
    is_exact_resumable_service_session,
    select_resumable_provider_session_id,
)
from pycastle.services.agent_service import AgentService


@dataclass
class _FakeAgentService:
    relpath: str | None
    name: str = "fake"
    resumable: bool = False
    provider_session_id: str | None = None
    persist_provider_session_id: bool = False
    exact_transcript_session: bool = False

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return self.resumable

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        if self.name == "claude":
            provider_session_id = (
                self.provider_session_id or request.role_session.session_uuid()
            )
            exact_transcript_match = False
            if request.require_exact_transcript_match:
                exact_transcript_match = self.exact_transcript_session or (
                    is_exact_resumable_service_session(
                        request.role_session,
                        self.name,
                        provider_session_id=provider_session_id,
                        provider_state_dir=request.provider_state_dir,
                    )
                )
            return ProviderSessionState(
                RunKind.RESUME
                if request.has_resumable_provider_state
                else RunKind.FRESH,
                provider_session_id,
                exact_transcript_match=exact_transcript_match,
                persist_provider_session_id=self.persist_provider_session_id,
            )
        if self.name == "codex":
            return CodexService().provider_session_state(request)
        if not request.has_resumable_provider_state:
            return ProviderSessionState(RunKind.FRESH, None)
        selection = select_resumable_provider_session_id(
            request.role_session,
            self.name,
            provider_state_dir=request.provider_state_dir,
            has_resumable_provider_state=request.has_resumable_provider_state,
        )
        if selection.provider_session_id is None:
            return ProviderSessionState(RunKind.FRESH, None)
        exact_transcript_match = False
        if request.require_exact_transcript_match:
            exact_transcript_match = self.exact_transcript_session or (
                is_exact_resumable_service_session(
                    request.role_session,
                    self.name,
                    provider_session_id=selection.provider_session_id,
                    provider_state_dir=request.provider_state_dir,
                )
            )
        return ProviderSessionState(
            RunKind.RESUME,
            selection.provider_session_id,
            exact_transcript_match=exact_transcript_match,
            persist_provider_session_id=selection.persist_provider_session_id,
        )


def test_run_session_plan_uses_service_state_dir_for_namespaced_role(tmp_path: Path):
    service = cast(
        AgentService, _FakeAgentService(".pycastle-session/improve/main/codex/")
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )

    assert plan == RunSessionPlan(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
        run_kind=RunKind.FRESH,
        service_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_state_dir_relpath=".pycastle-session/improve/main/codex/",
        host_provider_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_session_id=None,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )


def test_plan_run_session_public_interface_preserves_namespaced_role_session_facts(
    tmp_path: Path,
):
    service = cast(
        AgentService, _FakeAgentService(".pycastle-session/improve/main/codex/")
    )

    plan = plan_run_session(
        RunSessionPlanRequest(
            role=AgentRole.IMPROVE,
            worktree=tmp_path,
            namespace="main",
            service=service,
        )
    )

    assert plan == RunSessionPlan(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
        run_kind=RunKind.FRESH,
        service_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_state_dir_relpath=".pycastle-session/improve/main/codex/",
        host_provider_state_dir=tmp_path / ".pycastle-session/improve/main/codex",
        provider_session_id=None,
        auth_seeding_requirement=AuthSeedingRequirement.NOT_REQUIRED,
        recovered_session_id_persistence=RecoveredSessionIdPersistence.SKIP,
    )


def test_run_session_plan_uses_selected_codex_provider_state_dir_layout_when_service_state_dir_is_custom(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.service_state_dir == tmp_path / "custom" / "codex-state"
    assert plan.provider_state_dir_relpath == "custom/codex-state"
    assert plan.host_provider_state_dir == tmp_path / "custom" / "codex-state"


def test_run_session_plan_uses_selected_opencode_provider_state_dir_for_fresh_container_path(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/opencode-state", name="opencode", resumable=True),
    )
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_state_dir_container_path("/home/agent/workspace") == (
        "/home/agent/workspace/custom/opencode-state/"
    )


def test_run_session_plan_keeps_none_service_state_dir_when_service_has_no_state_dir(
    tmp_path: Path,
):
    service = cast(AgentService, _FakeAgentService(None))

    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.service_state_dir is None
    assert plan.provider_session_id is None
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.SKIP


def test_run_session_plan_reports_fresh_for_claude_with_absent_or_empty_state_dir(
    tmp_path: Path,
):
    service = ClaudeService()
    expected_relpath = ".pycastle-session/implementer/claude/"
    expected_state_dir = tmp_path / ".pycastle-session/implementer/claude"
    expected_session_id = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()

    absent_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    expected_state_dir.mkdir(parents=True)

    empty_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert absent_plan.run_kind is RunKind.FRESH
    assert empty_plan.run_kind is RunKind.FRESH
    assert absent_plan.provider_state_dir_relpath == expected_relpath
    assert empty_plan.provider_state_dir_relpath == expected_relpath
    assert absent_plan.host_provider_state_dir == expected_state_dir
    assert empty_plan.host_provider_state_dir == expected_state_dir
    assert absent_plan.service_state_dir == expected_state_dir
    assert empty_plan.service_state_dir == expected_state_dir
    assert absent_plan.provider_session_id == expected_session_id
    assert empty_plan.provider_session_id == expected_session_id
    assert absent_plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert empty_plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED


def test_run_session_plan_reports_resume_for_claude_with_populated_state_dir(
    tmp_path: Path,
):
    service = ClaudeService()
    expected_state_dir = tmp_path / ".pycastle-session/implementer/claude"
    expected_session_id = RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
    expected_state_dir.mkdir(parents=True)
    (expected_state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.service_state_dir == expected_state_dir
    assert plan.provider_session_id == expected_session_id


def test_run_session_plan_preserves_claude_provider_session_persistence_from_service_run_state(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService(
            ".pycastle-session/implementer/claude/",
            name="claude",
            resumable=True,
            persist_provider_session_id=True,
        ),
    )
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert (
        plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.PERSIST
    )


def test_role_session_exact_transcript_handoff_for_service_reports_unrecoverable_codex_identity_when_rollout_thread_ids_conflict(
    tmp_path: Path,
):
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "05" / "28"
    dir_b = state_dir / "sessions" / "2026" / "05" / "29"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-id-old"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-id-new"}\n',
        encoding="utf-8",
    )

    handoff = role_session.exact_transcript_handoff_for_service(service)

    assert handoff.provider_identity.kind is ProviderIdentityKind.UNRECOVERABLE
    assert handoff.provider_identity.run_kind is RunKind.FRESH
    assert handoff.provider_identity.provider_session_id is None
    assert handoff.is_eligible is False


def test_run_session_plan_namespaces_claude_provider_session_identity_only_when_non_empty(
    tmp_path: Path,
):
    service = ClaudeService()

    main_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    issues_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="issues",
        service=service,
    )
    base_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert main_plan.provider_session_id != issues_plan.provider_session_id
    assert main_plan.service_state_dir != issues_plan.service_state_dir
    assert (
        base_plan.provider_session_id
        == RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
    )
    assert (
        base_plan.provider_session_id
        == RoleSession(tmp_path, AgentRole.IMPLEMENTER, "").session_uuid()
    )


def test_run_session_plan_uses_namespaced_claude_provider_state_dir_paths(
    tmp_path: Path,
):
    service = ClaudeService()

    main_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=service,
    )
    issues_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="issues",
        service=service,
    )

    assert (
        main_plan.provider_state_dir_relpath == ".pycastle-session/improve/main/claude/"
    )
    assert main_plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "main" / "claude"
    )
    assert main_plan.provider_session_id == (
        RoleSession(tmp_path, AgentRole.IMPROVE, "main").session_uuid()
    )
    assert issues_plan.provider_state_dir_relpath == (
        ".pycastle-session/improve/issues/claude/"
    )
    assert issues_plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "improve" / "issues" / "claude"
    )
    assert issues_plan.provider_session_id == (
        RoleSession(tmp_path, AgentRole.IMPROVE, "issues").session_uuid()
    )


def test_run_session_plan_never_generates_pycastle_uuid_for_fresh_codex(
    tmp_path: Path,
):
    service = CodexService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None


def test_run_session_plan_never_generates_pycastle_uuid_for_fresh_opencode(
    tmp_path: Path,
):
    service = OpenCodeService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None


def test_codex_provider_session_state_returns_resume_decision_for_saved_sidecar(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )
    role_session.save_service_session_id("codex", "thread-from-sidecar")

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )

    assert decision.run_kind is RunKind.RESUME
    assert decision.provider_session_id == "thread-from-sidecar"
    assert (
        getattr(decision, "state_dir_relpath") == ".pycastle-session/implementer/codex/"
    )
    assert getattr(decision, "state_dir_path") == state_dir
    assert (
        getattr(decision, "auth_seeding_requirement") is AuthSeedingRequirement.REQUIRED
    )
    action = getattr(decision, "auth_seed_action")
    assert action is not None
    assert action.source == Path.home() / ".codex" / "auth.json"
    assert action.destination == state_dir / "auth.json"


def test_codex_provider_session_state_recovers_unique_rollout_and_persists_sidecar(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-from-rollout"}',
                '{"type":"thread.started","thread_id":"thread-from-rollout"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )

    assert decision.run_kind is RunKind.RESUME
    assert decision.provider_session_id == "thread-from-rollout"
    assert role_session.service_session_id("codex") == "thread-from-rollout"
    assert decision.persist_provider_session_id is True
    assert decision.state_dir_relpath == ".pycastle-session/implementer/codex/"
    assert decision.state_dir_path == state_dir


def test_codex_provider_session_state_returns_fresh_for_ambiguous_rollouts(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-a"}\n',
        encoding="utf-8",
    )
    (sessions_dir / "rollout-002.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-b"}\n',
        encoding="utf-8",
    )

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )

    assert decision.run_kind is RunKind.FRESH
    assert decision.provider_session_id is None
    assert role_session.service_session_id("codex") is None
    assert decision.state_dir_relpath == ".pycastle-session/implementer/codex/"
    assert decision.state_dir_path == state_dir
    assert decision.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED


def test_codex_provider_session_state_exposes_auth_seed_action_for_fresh_execution(
    tmp_path: Path,
) -> None:
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=RoleSession(tmp_path, AgentRole.IMPLEMENTER),
            provider_state_dir=state_dir,
            has_resumable_provider_state=False,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )

    assert decision.run_kind is RunKind.FRESH
    assert decision.provider_session_id is None
    assert decision.state_dir_relpath == ".pycastle-session/implementer/codex/"
    assert decision.state_dir_path == state_dir
    assert decision.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
    action = decision.auth_seed_action
    assert action is not None
    assert action.source == Path.home() / ".codex" / "auth.json"
    assert action.destination == state_dir / "auth.json"


def test_run_session_plan_requires_auth_seeding_for_fresh_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED


def test_run_session_plan_exposes_auth_seed_action_for_fresh_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    action = plan.auth_seed_action

    assert action is not None
    assert action.source == Path.home() / ".codex" / "auth.json"
    assert action.destination == (
        tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    )


def test_run_session_plan_skips_auth_seed_action_for_fresh_codex_with_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    state_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.auth_seed_action is None


@pytest.mark.parametrize(
    ("service", "role", "namespace", "expected_session_id"),
    [
        (ClaudeService(), AgentRole.PLANNER, "", "claude-session-id"),
        (CodexService(), AgentRole.IMPLEMENTER, "", "thread-codex-123"),
        (OpenCodeService(), AgentRole.IMPROVE, "main", "sess-opencode-123"),
    ],
)
def test_run_session_plan_records_service_session_metadata_on_success(
    tmp_path: Path,
    service: AgentService,
    role: AgentRole,
    namespace: str,
    expected_session_id: str,
):
    plan = RunSessionPlan.for_service(
        role=role,
        worktree=tmp_path,
        namespace=namespace,
        service=service,
    )

    plan.record_successful_run(expected_session_id)

    assert RoleSession(tmp_path, role, namespace).service_session_metadata(
        service.name
    ) == {
        "service": service.name,
        "provider_session_id": expected_session_id,
    }


def test_run_session_plan_captures_codex_provider_session_id_for_same_plan_reuse(
    tmp_path: Path,
):
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=CodexService(),
    )

    plan.capture_provider_session_id("thread-codex-456")
    plan.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert plan.provider_session_id == "thread-codex-456"
    assert role_session.service_session_id("codex") == "thread-codex-456"
    assert role_session.service_session_metadata("codex") == {
        "service": "codex",
        "provider_session_id": "thread-codex-456",
    }


def test_run_session_plan_captures_opencode_provider_session_id_for_same_plan_reuse(
    tmp_path: Path,
):
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=OpenCodeService(),
    )

    plan.capture_provider_session_id("sess-opencode-456")
    plan.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")
    assert plan.provider_session_id == "sess-opencode-456"
    assert role_session.service_session_id("opencode") == "sess-opencode-456"
    assert role_session.service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-opencode-456",
    }


def test_run_session_plan_requires_auth_seeding_for_resume_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED


def test_run_session_plan_exposes_auth_seed_action_for_resume_codex_without_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    action = plan.auth_seed_action

    assert plan.run_kind is RunKind.RESUME
    assert action is not None
    assert action.source == Path.home() / ".codex" / "auth.json"
    assert action.destination == state_dir / "auth.json"


def test_run_session_plan_skips_auth_seeding_for_resume_codex_with_auth_json(
    tmp_path: Path,
):
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.auth_seed_action is None


def test_run_session_plan_skips_auth_seed_action_when_selected_codex_state_dir_already_has_auth_json(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )
    state_dir = tmp_path / "custom" / "codex-state"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    (state_dir / "auth.json").write_text(
        '{"mode":"oauth","origin":"provider"}',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.auth_seed_action is None


def test_local_auth_seed_action_applies_only_to_preserved_codex_provider_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    service = cast(
        AgentService,
        _FakeAgentService("custom/codex-state", name="codex", resumable=True),
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    action = plan.auth_seed_action

    assert action is not None

    action.apply()

    provider_auth = tmp_path / "custom" / "codex-state" / "auth.json"
    assert (
        provider_auth.read_text(encoding="utf-8") == '{"mode":"oauth","origin":"host"}'
    )


def test_local_auth_seed_action_does_not_overwrite_existing_provider_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    service = CodexService()
    provider_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    provider_state_dir.mkdir(parents=True)
    provider_auth = provider_state_dir / "auth.json"
    provider_auth.write_text('{"mode":"oauth","origin":"provider"}', encoding="utf-8")

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.auth_seed_action is None

    LocalAuthSeedAction(
        source=host_auth,
        destination=provider_auth,
    ).apply()

    assert (
        provider_auth.read_text(encoding="utf-8")
        == '{"mode":"oauth","origin":"provider"}'
    )


def test_local_auth_seed_action_copies_only_host_auth_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    host_codex_dir = home / ".codex"
    host_codex_dir.mkdir(parents=True)
    host_auth = host_codex_dir / "auth.json"
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    (host_codex_dir / "config.toml").write_text("model = 'gpt-5.5'\n", encoding="utf-8")
    host_sessions_dir = host_codex_dir / "sessions"
    host_sessions_dir.mkdir()
    (host_sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"host-thread"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    destination = tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    LocalAuthSeedAction(source=host_auth, destination=destination).apply()

    assert destination.read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"host"}'
    )
    provider_state_dir = destination.parent
    assert not (provider_state_dir / "config.toml").exists()
    assert not (provider_state_dir / "sessions").exists()


def test_local_auth_seed_action_preserves_host_auth_file_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "host" / "auth.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    source.chmod(0o600)

    destination = tmp_path / "provider" / "auth.json"

    LocalAuthSeedAction(source=source, destination=destination).apply()

    assert destination.read_text(encoding="utf-8") == (
        '{"mode":"oauth","origin":"host"}'
    )
    assert destination.stat().st_mode & 0o777 == source.stat().st_mode & 0o777


def test_local_auth_seed_action_require_source_raises_hard_agent_error_when_missing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "auth.json"
    action = LocalAuthSeedAction(source=missing, destination=tmp_path / "dest.json")

    with pytest.raises(HardAgentError) as exc_info:
        action.require_source()

    assert exc_info.value.status_code == 401


def test_run_session_plan_capture_without_record_persists_session_id_but_not_metadata(
    tmp_path: Path,
) -> None:
    plan = RunSessionPlan.for_service(
        role=AgentRole.PLANNER,
        worktree=tmp_path,
        namespace="",
        service=OpenCodeService(),
    )

    plan.capture_provider_session_id("sess-captured")

    role_session = RoleSession(tmp_path, AgentRole.PLANNER)
    assert role_session.service_session_id("opencode") == "sess-captured"
    assert role_session.service_session_metadata("opencode") is None
