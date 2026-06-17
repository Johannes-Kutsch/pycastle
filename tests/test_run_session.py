from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle_agent_runtime.session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.errors import AgentCredentialFailureError
from pycastle.services import ClaudeService
from pycastle.services.codex_service import CodexService
from pycastle.services.opencode_service import OpenCodeService
from pycastle.session.agent import RunSessionPlanRequest, plan_run_session
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
    RecoveredSessionIdPersistence,
    RunSessionPlan,
)
from pycastle.session import (
    RoleSession,
    RunKind,
    has_exact_transcript_match,
    provider_state_relpath,
)
from pycastle.session._provider_session_plan import record_observed_provider_session_id
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

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        if self.name == "claude":
            return ProviderSessionPreferences(
                preferred_provider_session_id="unused-session-uuid"
            )
        return ProviderSessionPreferences()

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


@dataclass
class _FakeResumeIdentityStore:
    provider_session_id: str | None = None

    def session_uuid(self) -> str:
        return "unused-session-uuid"

    def service_session_id(self, service_name: str) -> str | None:
        del service_name
        return self.provider_session_id

    def save_service_session_id(self, service_name: str, session_id: str) -> None:
        del service_name
        self.provider_session_id = session_id

    def service_session_metadata(self, service_name: str) -> dict[str, str] | None:
        del service_name
        return None

    def exact_transcript_service_name(self) -> str | None:
        return None


@dataclass
class _CodexWithoutAuthPolicyService:
    relpath: str = ".pycastle-session/implementer/codex/"
    resumable: bool = False
    provider_session_id: str | None = None
    name: str = "codex"

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        del state_dir
        return self.resumable

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        run_kind = (
            RunKind.RESUME if request.has_resumable_provider_state else RunKind.FRESH
        )
        return ProviderSessionState(
            run_kind,
            self.provider_session_id,
            state_dir_relpath=request.state_dir_relpath,
            state_dir_path=request.provider_state_dir,
        )


class _ClaudeExecutionOnlyService(ClaudeService):
    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        raise AssertionError(
            "Claude provider session adapter should supply preferences"
        )

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        del request
        raise AssertionError("Claude provider session adapter should supply state")


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


def test_provider_state_relpath_formats_role_namespace_and_provider_name() -> None:
    assert (
        RoleSession.provider_state_relpath_for(AgentRole.IMPLEMENTER, "codex")
        == ".pycastle-session/implementer/codex/"
    )
    assert (
        RoleSession.provider_state_relpath_for(AgentRole.IMPROVE, "codex", "main")
        == ".pycastle-session/improve/main/codex/"
    )
    assert RoleSession.provider_state_relpath_for(
        AgentRole.IMPLEMENTER, "claude", ""
    ) == (RoleSession.provider_state_relpath_for(AgentRole.IMPLEMENTER, "claude"))
    assert (
        provider_state_relpath(AgentRole.IMPLEMENTER, "codex")
        == ".pycastle-session/implementer/codex/"
    )
    assert (
        provider_state_relpath(AgentRole.IMPROVE, "codex", "main")
        == ".pycastle-session/improve/main/codex/"
    )
    assert (
        provider_state_relpath(AgentRole.IMPROVE, "codex", "")
        == ".pycastle-session/improve/codex/"
    )
    assert provider_state_relpath(AgentRole.IMPLEMENTER, "claude", "") == (
        provider_state_relpath(AgentRole.IMPLEMENTER, "claude")
    )
    assert RoleSession.provider_state_relpath_for(
        AgentRole.IMPLEMENTER, "opencode"
    ) == (".pycastle-session/implementer/opencode/")
    assert RoleSession.provider_state_relpath_for(
        AgentRole.IMPROVE, "opencode", "main"
    ) == (".pycastle-session/improve/main/opencode/")
    assert RoleSession.provider_state_relpath_for(
        AgentRole.IMPLEMENTER, "opencode", ""
    ) == (RoleSession.provider_state_relpath_for(AgentRole.IMPLEMENTER, "opencode"))
    assert provider_state_relpath(AgentRole.IMPLEMENTER, "codex").endswith("/")


def test_role_session_provider_state_dir_matches_worktree_local_provider_layout(
    tmp_path: Path,
) -> None:
    assert RoleSession(tmp_path, AgentRole.IMPLEMENTER).provider_state_dir("codex") == (
        tmp_path / ".pycastle-session" / "implementer" / "codex"
    )
    assert RoleSession(
        tmp_path,
        AgentRole.IMPROVE,
        "main",
    ).provider_state_dir("opencode") == (
        tmp_path / ".pycastle-session" / "improve" / "main" / "opencode"
    )


def test_role_session_provider_state_relpath_matches_worktree_local_provider_layout(
    tmp_path: Path,
) -> None:
    role_session = RoleSession(tmp_path, AgentRole.IMPROVE, "main")

    assert role_session.provider_state_relpath("opencode") == (
        ".pycastle-session/improve/main/opencode"
    )
    assert role_session.provider_state_dir("opencode") == (
        tmp_path / role_session.provider_state_relpath("opencode")
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


def test_run_session_plan_uses_claude_provider_session_adapter_for_fresh_identity(
    tmp_path: Path,
) -> None:
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=cast(AgentService, _ClaudeExecutionOnlyService()),
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id == (
        RoleSession(tmp_path, AgentRole.IMPLEMENTER).session_uuid()
    )
    assert plan.provider_state_dir_relpath == ".pycastle-session/implementer/claude/"
    assert plan.host_provider_state_dir == (
        tmp_path / ".pycastle-session" / "implementer" / "claude"
    )


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


def test_run_session_plan_uses_claude_provider_session_adapter_for_resume_exact_transcript_match(
    tmp_path: Path,
) -> None:
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    role_session.save_service_session_metadata("claude", role_session.session_uuid())

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=cast(AgentService, _ClaudeExecutionOnlyService()),
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == role_session.session_uuid()
    assert plan.exact_transcript_match is True


def test_run_session_plan_preserves_claude_container_state_dir_path_without_namespace(
    tmp_path: Path,
) -> None:
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=ClaudeService(),
    )

    assert plan.provider_state_dir_container_path("/home/agent/workspace") == (
        "/home/agent/workspace/.pycastle-session/implementer/claude/"
    )


def test_run_session_plan_preserves_claude_container_state_dir_path_with_namespace(
    tmp_path: Path,
) -> None:
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPROVE,
        worktree=tmp_path,
        namespace="main",
        service=ClaudeService(),
    )

    assert plan.provider_state_dir_container_path("/home/agent/workspace") == (
        "/home/agent/workspace/.pycastle-session/improve/main/claude/"
    )


def test_claude_provider_session_state_uses_preferred_session_id_from_request_contract(
    tmp_path: Path,
) -> None:
    service = ClaudeService()
    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=role_session,
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/claude/",
            preferred_provider_session_id="preferred-id",
        )
    )

    assert decision.run_kind is RunKind.RESUME
    assert decision.provider_session_id == "preferred-id"
    assert decision.state_dir_relpath == ".pycastle-session/implementer/claude/"
    assert decision.state_dir_path == state_dir


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


def test_plan_run_session_reports_unrecoverable_codex_identity_when_rollout_thread_ids_conflict(
    tmp_path: Path,
):
    service = CodexService()
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

    plan = plan_run_session(
        RunSessionPlanRequest(
            role=AgentRole.IMPLEMENTER,
            worktree=tmp_path,
            namespace="",
            service=service,
        )
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.provider_session_id is None
    assert plan.exact_transcript_match is False


def test_has_exact_transcript_match_accepts_sidecar_backed_opencode_handoff_without_state_dir_session_id_sidecar(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService(
            "custom/opencode-state/",
            name="opencode",
            resumable=True,
        ),
    )
    role_session = RoleSession(tmp_path, AgentRole.REVIEWER, "main")
    state_dir = tmp_path / "custom" / "opencode-state"
    state_dir.mkdir(parents=True)
    (state_dir / "resume.jsonl").write_text("{}\n", encoding="utf-8")
    role_session.save_service_session_id("opencode", "sess-opencode-123")
    role_session.save_service_session_metadata("opencode", "sess-opencode-123")

    assert (
        has_exact_transcript_match(
            worktree=tmp_path,
            role=AgentRole.REVIEWER,
            session_namespace="main",
            service=service,
        )
        is True
    )
    assert (
        plan_run_session(
            RunSessionPlanRequest(
                role=AgentRole.REVIEWER,
                worktree=tmp_path,
                namespace="main",
                service=service,
            )
        ).exact_transcript_match
        is True
    )


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


def test_run_session_plan_rotates_claude_provider_session_identity_when_role_session_evidence_is_missing(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "pycastle" / ".worktrees" / "issue-1"
    worktree.mkdir(parents=True)
    (worktree / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    service = ClaudeService()
    role_session = RoleSession(worktree, AgentRole.IMPLEMENTER)

    initial_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=worktree,
        namespace="",
        service=service,
    )
    assert initial_plan.provider_session_id is not None

    state_dir = worktree / ".pycastle-session" / "implementer" / "claude"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    role_session.save_service_session_metadata(
        "claude", initial_plan.provider_session_id
    )

    resumed_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=worktree,
        namespace="",
        service=service,
    )

    shutil.rmtree(worktree)
    worktree.mkdir(parents=True)
    (worktree / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")

    recovered_branch_only_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=worktree,
        namespace="",
        service=service,
    )

    assert resumed_plan.run_kind is RunKind.RESUME
    assert resumed_plan.provider_session_id == initial_plan.provider_session_id
    assert recovered_branch_only_plan.run_kind is RunKind.FRESH
    assert (
        recovered_branch_only_plan.provider_session_id
        != initial_plan.provider_session_id
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


def test_opencode_provider_session_state_returns_fresh_without_state_dir_session_id(
    tmp_path: Path,
) -> None:
    service = OpenCodeService()
    resume_identity = _FakeResumeIdentityStore(provider_session_id="sess-stale")
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "opencode"
    state_dir.mkdir(parents=True)

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=resume_identity,
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/opencode/",
        )
    )

    assert decision.run_kind is RunKind.FRESH
    assert decision.provider_session_id is None
    assert decision.state_dir_relpath == ".pycastle-session/implementer/opencode/"
    assert decision.state_dir_path == state_dir


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


def test_run_session_plan_persists_recovered_codex_rollout_thread_id(
    tmp_path: Path,
) -> None:
    service = CodexService()
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-from-rollout"
    assert (
        plan.recovered_session_id_persistence is RecoveredSessionIdPersistence.PERSIST
    )
    assert (
        RoleSession(tmp_path, AgentRole.IMPLEMENTER).service_session_id("codex")
        == "thread-from-rollout"
    )


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


def test_run_session_plan_does_not_require_auth_seeding_for_fresh_codex_without_service_auth_policy(
    tmp_path: Path,
):
    service = cast(AgentService, _CodexWithoutAuthPolicyService("custom/codex-state"))

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.FRESH
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.auth_seed_action is None


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
    assert (
        action.missing_source_message
        == "Codex authentication missing: run `codex login` on the host."
    )
    assert action.missing_source_service_name == "codex"
    assert action.missing_source_status_code == 401
    assert len(action.missing_source_observations) == 1
    observation = action.missing_source_observations[0]
    assert observation.service_name == "codex"
    assert (
        observation.raw_provider_text
        == "Codex authentication missing: run `codex login` on the host."
    )
    assert observation.source_stream == "pre-dispatch host check"
    assert observation.status_code == 401


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


@pytest.mark.parametrize(
    ("service", "role", "namespace", "runtime_session_id"),
    [
        (CodexService(), AgentRole.IMPLEMENTER, "", "thread-codex-runtime"),
        (OpenCodeService(), AgentRole.IMPROVE, "main", "sess-opencode-runtime"),
    ],
)
def test_run_session_plan_records_runtime_provider_session_id_in_sidecar_and_metadata_on_success(
    tmp_path: Path,
    service: AgentService,
    role: AgentRole,
    namespace: str,
    runtime_session_id: str,
) -> None:
    plan = RunSessionPlan.for_service(
        role=role,
        worktree=tmp_path,
        namespace=namespace,
        service=service,
    )

    plan.record_successful_run(runtime_session_id)

    role_session = RoleSession(tmp_path, role, namespace)
    assert role_session.service_session_id(service.name) == runtime_session_id
    assert role_session.service_session_metadata(service.name) == {
        "service": service.name,
        "provider_session_id": runtime_session_id,
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


def test_record_observed_provider_session_id_persists_codex_thread_id_sidecar(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"

    record_observed_provider_session_id(
        worktree=tmp_path,
        role=AgentRole.IMPLEMENTER,
        namespace="",
        service_name="codex",
        service_state_dir=state_dir,
        provider_session_id="thread-codex-456",
    )

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert role_session.service_session_id("codex") == "thread-codex-456"
    assert (state_dir / "thread_id").read_text(encoding="utf-8") == ("thread-codex-456")


def test_record_observed_provider_session_id_persists_generic_service_session_id(
    tmp_path: Path,
):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "fake"

    record_observed_provider_session_id(
        worktree=tmp_path,
        role=AgentRole.IMPLEMENTER,
        namespace="",
        service_name="fake",
        service_state_dir=state_dir,
        provider_session_id="thread-fake-456",
    )

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert role_session.service_session_id("fake") == "thread-fake-456"


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


def test_run_session_plan_captures_generic_provider_session_id_for_same_plan_reuse(
    tmp_path: Path,
):
    service = cast(
        AgentService,
        _FakeAgentService(".pycastle-session/implementer/fake/"),
    )
    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    plan.capture_provider_session_id("thread-fake-456")
    plan.record_successful_run()

    role_session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert plan.provider_session_id == "thread-fake-456"
    assert role_session.service_session_id("fake") == "thread-fake-456"
    assert role_session.service_session_metadata("fake") == {
        "service": "fake",
        "provider_session_id": "thread-fake-456",
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


def test_run_session_plan_does_not_require_auth_seeding_for_resume_codex_without_service_auth_policy(
    tmp_path: Path,
):
    state_dir = tmp_path / "custom" / "codex-state"
    (state_dir / "sessions").mkdir(parents=True)
    service = cast(
        AgentService,
        _CodexWithoutAuthPolicyService(
            relpath="custom/codex-state",
            resumable=True,
            provider_session_id="thread-resume",
        ),
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-resume"
    assert plan.auth_seeding_requirement is AuthSeedingRequirement.NOT_REQUIRED
    assert plan.auth_seed_action is None


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


def test_run_session_plan_skips_auth_seeding_for_resume_codex_with_provider_auth_without_service_auth_policy(
    tmp_path: Path,
):
    state_dir = tmp_path / "custom" / "codex-state"
    (state_dir / "sessions").mkdir(parents=True)
    (state_dir / "auth.json").write_text(
        '{"mode":"oauth","origin":"provider"}',
        encoding="utf-8",
    )
    service = cast(
        AgentService,
        _CodexWithoutAuthPolicyService(
            relpath="custom/codex-state",
            resumable=True,
            provider_session_id="thread-resume",
        ),
    )

    plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="",
        service=service,
    )

    assert plan.run_kind is RunKind.RESUME
    assert plan.provider_session_id == "thread-resume"
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


def test_local_auth_seed_action_require_source_raises_agent_credential_failure_when_missing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "auth.json"
    action = LocalAuthSeedAction(source=missing, destination=tmp_path / "dest.json")

    with pytest.raises(AgentCredentialFailureError) as exc_info:
        action.require_source()

    assert exc_info.value.status_code == 401
    assert exc_info.value.service_name == "codex"
    assert len(exc_info.value.observations) == 1
    observation = exc_info.value.observations[0]
    assert observation.source_stream == "pre-dispatch host check"
    assert observation.status_code == 401


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
