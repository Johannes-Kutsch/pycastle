"""Tests for session_resume: RoleSession lifecycle and stage/session helpers."""

from __future__ import annotations

import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.runtime_session import (
    session_uuid as runtime_session_uuid,
)
from pycastle.services import ServiceRegistry
from pycastle.services.runtime_services import (
    AgentService,
    CodexService,
)
from pycastle.session import (
    RoleSession,
    RunKind,
    any_role_dir_present,
    is_stage_done_for,
)
from pycastle.session.service_session_store import (
    ServiceSessionStore,
    has_exact_transcript,
)


def _role_session_service_session_id(
    role_session: object,
    service_name: str,
) -> str | None:
    role_session_path = getattr(role_session, "path", None)
    if isinstance(role_session_path, Path):
        saved_session_id = ServiceSessionStore(
            role_session_path
        ).get_service_session_id(service_name)
        if saved_session_id is not None:
            return saved_session_id
    legacy = getattr(role_session, "service_session_id", None)
    if callable(legacy):
        return legacy(service_name)
    return None


@dataclass(frozen=True)
class _FakeService:
    name: str
    relpath: str | None
    resumable: bool

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        return self.relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return self.resumable

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        return ProviderSessionPreferences(
            preferred_provider_session_id=request.preferred_provider_session_id
        )

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        if self.name == "claude":
            return ProviderSessionState(
                RunKind.RESUME
                if request.has_resumable_provider_state
                else RunKind.FRESH,
                request.preferred_provider_session_id,
            )
        if not request.has_resumable_provider_state:
            return ProviderSessionState(RunKind.FRESH, None)
        saved_provider_session_id = _role_session_service_session_id(
            request.role_session, self.name
        )
        if saved_provider_session_id is None:
            return ProviderSessionState(RunKind.FRESH, None)
        return ProviderSessionState(RunKind.RESUME, saved_provider_session_id)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def worktree(tmp_path):
    return tmp_path


@pytest.fixture
def rs(worktree):
    return RoleSession(worktree, AgentRole.IMPLEMENTER)


# ── session_uuid determinism ──────────────────────────────────────────────────


def test_session_uuid_is_deterministic(worktree):
    assert runtime_session_uuid(
        worktree, AgentRole.IMPLEMENTER.value, ""
    ) == runtime_session_uuid(worktree, AgentRole.IMPLEMENTER.value, "")


def test_session_uuid_differs_by_role(worktree):
    assert runtime_session_uuid(
        worktree, AgentRole.IMPLEMENTER.value, ""
    ) != runtime_session_uuid(worktree, AgentRole.REVIEWER.value, "")


def test_session_uuid_differs_by_worktree(tmp_path):
    a = runtime_session_uuid(tmp_path / "issue-1", AgentRole.IMPLEMENTER.value, "")
    b = runtime_session_uuid(tmp_path / "issue-2", AgentRole.IMPLEMENTER.value, "")
    assert a != b


def test_session_uuid_differs_by_namespace(worktree):
    a = runtime_session_uuid(worktree, AgentRole.IMPROVE.value, "main")
    b = runtime_session_uuid(worktree, AgentRole.IMPROVE.value, "issues")
    assert a != b


def test_session_uuid_empty_namespace_equals_no_namespace(worktree):
    assert runtime_session_uuid(
        worktree, AgentRole.IMPLEMENTER.value, ""
    ) == runtime_session_uuid(worktree, AgentRole.IMPLEMENTER.value, "")


def test_session_uuid_resolved_path_equals_direct(worktree):
    assert runtime_session_uuid(
        worktree, AgentRole.IMPLEMENTER.value, ""
    ) == runtime_session_uuid(worktree.resolve(), AgentRole.IMPLEMENTER.value, "")


def test_session_uuid_is_valid_uuid_string(worktree):
    result = runtime_session_uuid(worktree, AgentRole.IMPLEMENTER.value, "")
    assert str(uuid.UUID(result)) == result


# ── RoleSession lifecycle ─────────────────────────────────────────────────────


def test_fresh_worktree_reports_fresh(rs):
    assert rs.run_kind() == RunKind.FRESH
    assert rs.is_resumable() is False
    assert rs.is_done() is False


def test_continuation_file_controls_resumable_state(rs):
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n", encoding="utf-8")

    assert rs.is_resumable() is False

    (rs.path / "_continuation").write_text("opaque-token", encoding="utf-8")

    assert rs.run_kind() == RunKind.RESUME
    assert rs.is_resumable() is True
    assert rs.is_done() is False


def test_populated_dir_without_done_is_not_resumable_or_done(rs):
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n", encoding="utf-8")

    assert rs.run_kind() == RunKind.FRESH
    assert rs.is_resumable() is False
    assert rs.is_done() is False


def test_completion_signal_done_dir_survives_next_session_is_fresh(rs, worktree):
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n")
    rs.clear_provider_state_and_signal_completion()

    assert rs.is_done() is True
    assert rs.is_resumable() is False
    assert rs.path.is_dir()
    assert RoleSession(worktree, AgentRole.IMPLEMENTER).run_kind() == RunKind.FRESH


def test_completion_signal_removes_readonly_files(rs):
    rs.start_fresh()
    pack_dir = rs.path / "codex" / ".tmp" / "plugins" / ".git" / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    pack_file = pack_dir / "pack-abc123.pack"
    pack_file.write_bytes(b"data")
    pack_file.chmod(stat.S_IREAD)

    rs.clear_provider_state_and_signal_completion()

    assert rs.is_done() is True
    assert rs.is_resumable() is False


def test_start_fresh_on_populated_dir_makes_not_resumable(rs):
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n")
    rs.start_fresh()

    assert rs.is_resumable() is False


def test_start_fresh_recreates_empty_session_store(rs):
    rs.start_fresh()
    (rs.path / "_continuation").write_text("opaque-token", encoding="utf-8")
    (rs.path / "nested").mkdir()
    (rs.path / "nested" / "state.json").write_text("{}", encoding="utf-8")

    rs.start_fresh()

    assert rs.path.is_dir()
    assert list(rs.path.iterdir()) == []
    assert rs.is_done() is False


def test_continuation_round_trips_via_role_session_methods(rs):
    rs.write_continuation("serialized-state")

    assert rs.is_resumable() is True
    assert rs.read_continuation() == "serialized-state"


def test_service_session_ids_are_isolated_by_role_and_worktree(tmp_path):
    planner_a = RoleSession(tmp_path / "worktree-a", AgentRole.PLANNER)
    planner_b = RoleSession(tmp_path / "worktree-b", AgentRole.PLANNER)
    reviewer_a = RoleSession(tmp_path / "worktree-a", AgentRole.REVIEWER)

    ServiceSessionStore(planner_a.path).save_service_session_id("opencode", "sess-a")
    ServiceSessionStore(planner_b.path).save_service_session_id("opencode", "sess-b")
    ServiceSessionStore(reviewer_a.path).save_service_session_id(
        "opencode", "sess-review"
    )

    assert _role_session_service_session_id(planner_a, "opencode") == "sess-a"
    assert _role_session_service_session_id(planner_b, "opencode") == "sess-b"
    assert _role_session_service_session_id(reviewer_a, "opencode") == "sess-review"


def test_service_session_ids_use_service_specific_sidecars(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)

    ServiceSessionStore(rs.path).save_service_session_id("codex", "thread-123")
    ServiceSessionStore(rs.path).save_service_session_id("opencode", "sess-123")
    ServiceSessionStore(rs.path).save_service_session_id(
        "unknown-service", "default-123"
    )

    assert _role_session_service_session_id(rs, "codex") == "thread-123"
    assert _role_session_service_session_id(rs, "opencode") == "sess-123"
    assert _role_session_service_session_id(rs, "unknown-service") == "default-123"


def test_service_session_id_sidecars_follow_role_session_provider_state_layout(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")

    ServiceSessionStore(rs.path).save_service_session_id("codex", "thread-123")
    ServiceSessionStore(rs.path).save_service_session_id("opencode", "sess-123")
    ServiceSessionStore(rs.path).save_service_session_id(
        "unknown-service", "default-123"
    )

    assert (
        ServiceSessionStore(rs.path).session_id_path("codex")
        == rs.provider_state_dir("codex") / "thread_id"
    )
    assert (
        ServiceSessionStore(rs.path).session_id_path("opencode")
        == rs.provider_state_dir("opencode") / "session_id"
    )
    assert (
        ServiceSessionStore(rs.path).session_id_path("unknown-service")
        == rs.provider_state_dir("unknown-service") / "thread_id"
    )
    assert (
        worktree / ".pycastle-session" / "improve" / "main" / "codex" / "thread_id"
    ).read_text(encoding="utf-8") == "thread-123"
    assert (
        worktree / ".pycastle-session" / "improve" / "main" / "opencode" / "session_id"
    ).read_text(encoding="utf-8") == "sess-123"
    assert (
        worktree
        / ".pycastle-session"
        / "improve"
        / "main"
        / "unknown-service"
        / "thread_id"
    ).read_text(encoding="utf-8") == "default-123"


def test_completion_signal_clears_provider_state_and_marks_done(rs):
    rs.start_fresh()
    ServiceSessionStore(rs.path).save_service_session_id("codex", "thread-from-run")
    (rs.path / "_continuation").write_text("opaque", encoding="utf-8")

    rs.clear_provider_state_and_signal_completion()

    assert rs.is_done() is True
    assert rs.is_resumable() is False
    assert rs.run_kind() == RunKind.FRESH


_KNOWN = frozenset({"claude", "codex", "opencode"})


def test_transcript_owner_returns_none_when_session_dir_absent(tmp_path):
    store = ServiceSessionStore(tmp_path / "nonexistent")
    assert store.transcript_owner_service_name(_KNOWN) is None


def test_transcript_owner_returns_none_when_no_qualifying_subdirs(tmp_path):
    store = ServiceSessionStore(tmp_path)
    assert store.transcript_owner_service_name(_KNOWN) is None


def test_transcript_owner_returns_service_when_single_service_subdir_has_files(
    tmp_path,
):
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "thread_id").write_text("abc", encoding="utf-8")
    store = ServiceSessionStore(tmp_path)
    assert store.transcript_owner_service_name(_KNOWN) == "claude"


def test_transcript_owner_returns_none_when_service_subdir_is_empty(tmp_path):
    (tmp_path / "claude").mkdir()
    store = ServiceSessionStore(tmp_path)
    assert store.transcript_owner_service_name(_KNOWN) is None


def test_transcript_owner_returns_none_when_multiple_services_have_files(tmp_path):
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "thread_id").write_text("abc", encoding="utf-8")
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "thread_id").write_text("xyz", encoding="utf-8")
    store = ServiceSessionStore(tmp_path)
    assert store.transcript_owner_service_name(_KNOWN) is None


def test_transcript_owner_ignores_non_service_namespace_subdirs(tmp_path):
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "thread_id").write_text("abc", encoding="utf-8")
    (tmp_path / "candidate").mkdir()
    (tmp_path / "candidate" / "some_file").write_text("data", encoding="utf-8")
    store = ServiceSessionStore(tmp_path)
    assert store.transcript_owner_service_name(_KNOWN) == "claude"


def test_transcript_owner_ignores_unknown_subdirs_with_files(tmp_path):
    (tmp_path / "unknown-service").mkdir()
    (tmp_path / "unknown-service" / "thread_id").write_text("abc", encoding="utf-8")
    store = ServiceSessionStore(tmp_path)
    assert store.transcript_owner_service_name(_KNOWN) is None


def test_transcript_owner_counts_nested_files_in_service_subdir(tmp_path):
    (tmp_path / "opencode").mkdir()
    nested = tmp_path / "opencode" / "nested"
    nested.mkdir()
    (nested / "session_id").write_text("sess-123", encoding="utf-8")
    store = ServiceSessionStore(tmp_path)
    assert store.transcript_owner_service_name(_KNOWN) == "opencode"


def test_role_session_reports_exact_provider_transcript_available_for_selected_opencode_service(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.REVIEWER, "main")
    service = _FakeService(
        name="opencode",
        relpath="custom/opencode-state/",
        resumable=True,
    )
    (rs.path / "opencode").mkdir(parents=True)
    (rs.path / "opencode" / "seed").write_text("seed", encoding="utf-8")
    registry = ServiceRegistry({"opencode": cast("AgentService", service)})

    _svc = registry["opencode"]
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.REVIEWER,
            namespace="main",
            service=_svc,
            known_service_names=frozenset(registry.services.keys()),
        )
        if _svc is not None
        else False
    ) is True
    _svc = registry["opencode"]
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.REVIEWER,
            namespace="main",
            service=_svc,
            known_service_names=frozenset(registry.services.keys()),
        )
        if _svc is not None
        else False
    ) is True


@pytest.mark.parametrize(
    ("registry_services", "selected_service_name"),
    [
        ({}, "codex"),
        (
            {
                "claude": _FakeService(
                    name="claude",
                    relpath="custom/claude-state/",
                    resumable=True,
                )
            },
            "claude",
        ),
    ],
)
def test_role_session_reports_exact_provider_transcript_unavailable_for_missing_or_different_selected_service(
    worktree,
    registry_services: dict[str, _FakeService],
    selected_service_name: str,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    state_dir = rs.path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    rollout_dir.joinpath("rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-exact"}\n',
        encoding="utf-8",
    )
    ServiceSessionStore(rs.path).save_service_session_id("codex", "thread-exact")
    registry = ServiceRegistry(cast("dict[str, AgentService]", registry_services))

    _svc = (
        None
        if (registry is None or not selected_service_name)
        else registry[selected_service_name]
    )
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=_svc,
            known_service_names=frozenset(registry.services.keys()),
        )
        if _svc is not None
        else False
    ) is False
    _svc = (
        None
        if (registry is None or not selected_service_name)
        else registry[selected_service_name]
    )
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=_svc,
            known_service_names=frozenset(registry.services.keys()),
        )
        if _svc is not None
        else False
    ) is False


def test_role_session_reports_exact_transcript_handoff_unavailable_for_ambiguous_ownership(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    codex_dir = rs.path / "codex"
    rollout_dir = codex_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    rollout_dir.joinpath("rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-exact"}\n',
        encoding="utf-8",
    )
    opencode_dir = rs.path / "opencode"
    opencode_dir.mkdir(parents=True)
    (opencode_dir / "seed").write_text("seed", encoding="utf-8")
    registry = ServiceRegistry({"codex": CodexService()})

    _svc = registry["codex"]
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=_svc,
            known_service_names=frozenset({"codex", "opencode"}),
        )
        if _svc is not None
        else False
    ) is False
    _svc = registry["codex"]
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=_svc,
            known_service_names=frozenset({"codex", "opencode"}),
        )
        if _svc is not None
        else False
    ) is False


@pytest.mark.parametrize(
    "scenario",
    [
        "no_service_dir",
        "two_service_dirs",
        "service_dir_not_resumable",
    ],
)
def test_role_session_reports_exact_provider_transcript_unavailable_without_exact_identity_evidence(
    worktree,
    scenario: str,
):
    rs = RoleSession(worktree, AgentRole.REVIEWER, "main")
    service = _FakeService(
        name="opencode",
        relpath="custom/opencode-state/",
        resumable=(scenario != "service_dir_not_resumable"),
    )
    if scenario in ("two_service_dirs", "service_dir_not_resumable"):
        svc_dir = rs.path / "opencode"
        svc_dir.mkdir(parents=True)
        (svc_dir / "seed").write_text("seed", encoding="utf-8")
    if scenario == "two_service_dirs":
        codex_dir = rs.path / "codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "seed").write_text("seed", encoding="utf-8")
    known = (
        frozenset({"opencode", "codex"})
        if scenario == "two_service_dirs"
        else frozenset({"opencode"})
    )

    _svc = ServiceRegistry({service.name: cast("AgentService", service)})[service.name]
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.REVIEWER,
            namespace="main",
            service=_svc,
            known_service_names=known,
        )
        if _svc is not None
        else False
    ) is False


def test_role_session_reports_exact_provider_transcript_codex_availability_for_single_and_ambiguous_ownership(
    worktree,
):
    rs = RoleSession(worktree, AgentRole.IMPROVE, "main")
    service = CodexService()
    state_dir = rs.path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    rollout_path = rollout_dir / "rollout-001.jsonl"

    rollout_path.write_text(
        '{"type":"thread.started","thread_id":"thread-exact"}\n',
        encoding="utf-8",
    )

    _svc = ServiceRegistry({service.name: service})[service.name]
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=_svc,
            known_service_names=frozenset({"codex"}),
        )
        if _svc is not None
        else False
    ) is True

    opencode_dir = rs.path / "opencode"
    opencode_dir.mkdir(parents=True)
    (opencode_dir / "seed").write_text("seed", encoding="utf-8")

    _svc = ServiceRegistry({service.name: service})[service.name]
    assert (
        has_exact_transcript(
            worktree=worktree,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=_svc,
            known_service_names=frozenset({"codex", "opencode"}),
        )
        if _svc is not None
        else False
    ) is False


# ── any_role_dir_present ──────────────────────────────────────────────────────


def test_any_role_dir_present_false_when_no_session_base(worktree):
    assert any_role_dir_present(worktree) is False


def test_any_role_dir_present_true_once_a_role_dir_exists(worktree):
    RoleSession(worktree, AgentRole.IMPLEMENTER).start_fresh()
    assert any_role_dir_present(worktree) is True


def test_any_role_dir_present_true_regardless_of_done_state(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    rs.start_fresh()
    rs.clear_provider_state_and_signal_completion()
    assert any_role_dir_present(worktree) is True


# ── is_stage_done_for ─────────────────────────────────────────────────────────


def test_is_stage_done_for_false_when_absent(worktree):
    assert is_stage_done_for(worktree, AgentRole.IMPLEMENTER) is False


def test_is_stage_done_for_true_after_completion_signal(worktree):
    rs = RoleSession(worktree, AgentRole.IMPLEMENTER)
    rs.start_fresh()
    (rs.path / "session.jsonl").write_text("{}\n")
    rs.clear_provider_state_and_signal_completion()
    assert is_stage_done_for(worktree, AgentRole.IMPLEMENTER) is True


# ── RoleSession.discard() ─────────────────────────────────────────────────────


def test_discard_after_start_fresh_removes_role_dir(rs, worktree):
    rs.start_fresh()
    rs.discard()

    assert rs.is_resumable() is False
    assert rs.is_done() is False
    assert any_role_dir_present(worktree) is False


def test_discard_removes_nested_contents(rs, worktree):
    rs.start_fresh()
    nested = rs.path / "subdir"
    nested.mkdir()
    (nested / "file.txt").write_text("data")
    rs.discard()

    assert rs.is_resumable() is False
    assert rs.is_done() is False


def test_discard_on_nonexistent_dir_is_noop(rs):
    rs.discard()  # no start_fresh — dir never created


def test_discard_is_idempotent(rs, worktree):
    rs.start_fresh()
    rs.discard()
    rs.discard()

    assert rs.is_resumable() is False
    assert rs.is_done() is False


def test_discard_sibling_safe(worktree):
    rs_impl = RoleSession(worktree, AgentRole.IMPLEMENTER)
    rs_review = RoleSession(worktree, AgentRole.REVIEWER)
    rs_impl.start_fresh()
    rs_review.start_fresh()
    rs_review.clear_provider_state_and_signal_completion()

    rs_impl.discard()

    assert any_role_dir_present(worktree) is True
    assert rs_review.is_resumable() is False
    assert rs_review.is_done() is True


def test_start_fresh_after_completion_clears_done_signal(rs):
    rs.start_fresh()
    rs.clear_provider_state_and_signal_completion()

    rs.start_fresh()

    assert rs.is_done() is False
    assert rs.is_resumable() is False


def test_discard_after_completion_clears_done_signal(rs):
    rs.start_fresh()
    rs.clear_provider_state_and_signal_completion()

    rs.discard()

    assert rs.is_done() is False
    assert rs.is_resumable() is False


# ── RoleSession fingerprint ───────────────────────────────────────────────────


def test_read_fingerprint_returns_none_when_absent(rs):
    assert rs.read_fingerprint() is None


def test_write_then_read_fingerprint_round_trips(rs):
    rs.write_fingerprint("abc123hash")

    assert rs.read_fingerprint() == "abc123hash"


def test_read_fingerprint_returns_none_after_discard(rs):
    rs.write_fingerprint("abc123hash")
    rs.discard()

    assert rs.read_fingerprint() is None


def test_read_fingerprint_returns_none_after_start_fresh(rs):
    rs.write_fingerprint("abc123hash")
    rs.start_fresh()

    assert rs.read_fingerprint() is None
