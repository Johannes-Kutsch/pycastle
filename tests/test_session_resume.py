"""Tests for session_resume: decide_agent_run_kind, has_resumable_session, derived_session_uuid."""

import uuid


from pycastle.agent_output_protocol import AgentRole
from pycastle.session_resume import (
    RunKind,
    decide_agent_run_kind,
    derived_session_uuid,
    has_resumable_session,
    is_stage_done,
)


# ── has_resumable_session ─────────────────────────────────────────────────────


def test_has_resumable_session_returns_false_when_dir_absent(tmp_path):
    assert has_resumable_session(tmp_path / "implementer") is False


def test_has_resumable_session_returns_false_when_dir_is_empty(tmp_path):
    role_dir = tmp_path / "implementer"
    role_dir.mkdir()
    assert has_resumable_session(role_dir) is False


def test_has_resumable_session_returns_true_when_dir_contains_a_file(tmp_path):
    role_dir = tmp_path / "implementer"
    role_dir.mkdir()
    (role_dir / "session.json").write_text("{}")
    assert has_resumable_session(role_dir) is True


def test_has_resumable_session_returns_true_for_nested_file(tmp_path):
    role_dir = tmp_path / "reviewer"
    (role_dir / "sub").mkdir(parents=True)
    (role_dir / "sub" / "data.json").write_text("{}")
    assert has_resumable_session(role_dir) is True


def test_has_resumable_session_returns_false_when_dir_has_only_empty_subdir(tmp_path):
    role_dir = tmp_path / "merger"
    (role_dir / "empty_sub").mkdir(parents=True)
    assert has_resumable_session(role_dir) is False


# ── is_stage_done ─────────────────────────────────────────────────────────────


def test_is_stage_done_returns_false_when_dir_absent(tmp_path):
    assert is_stage_done(tmp_path / "implementer") is False


def test_is_stage_done_returns_false_when_dir_has_session_content(tmp_path):
    role_dir = tmp_path / "implementer"
    role_dir.mkdir()
    (role_dir / "session.jsonl").write_text("{}\n")
    assert is_stage_done(role_dir) is False


def test_is_stage_done_returns_true_when_dir_present_and_empty(tmp_path):
    role_dir = tmp_path / "implementer"
    role_dir.mkdir()
    assert is_stage_done(role_dir) is True


# ── decide_agent_run_kind ─────────────────────────────────────────────────────


def test_decide_agent_run_kind_fresh_when_no_session():
    kind = decide_agent_run_kind(AgentRole.IMPLEMENTER, session_dir_present=False)
    assert kind == RunKind.FRESH


def test_decide_agent_run_kind_resume_when_session_present():
    kind = decide_agent_run_kind(AgentRole.IMPLEMENTER, session_dir_present=True)
    assert kind == RunKind.RESUME


def test_decide_agent_run_kind_fresh_for_reviewer_without_session():
    kind = decide_agent_run_kind(AgentRole.REVIEWER, session_dir_present=False)
    assert kind == RunKind.FRESH


def test_decide_agent_run_kind_resume_for_reviewer_with_session():
    kind = decide_agent_run_kind(AgentRole.REVIEWER, session_dir_present=True)
    assert kind == RunKind.RESUME


def test_decide_agent_run_kind_resume_for_merger_with_session():
    kind = decide_agent_run_kind(AgentRole.MERGER, session_dir_present=True)
    assert kind == RunKind.RESUME


def test_decide_agent_run_kind_fresh_for_improve_without_session():
    kind = decide_agent_run_kind(AgentRole.IMPROVE, session_dir_present=False)
    assert kind == RunKind.FRESH


def test_decide_agent_run_kind_resume_for_improve_with_session():
    kind = decide_agent_run_kind(AgentRole.IMPROVE, session_dir_present=True)
    assert kind == RunKind.RESUME


def test_decide_agent_run_kind_returns_run_kind_enum():
    kind = decide_agent_run_kind(AgentRole.IMPLEMENTER, session_dir_present=False)
    assert isinstance(kind, RunKind)


# ── derived_session_uuid ──────────────────────────────────────────────────────


def test_derived_session_uuid_returns_string(tmp_path):
    result = derived_session_uuid(AgentRole.IMPLEMENTER, tmp_path)
    assert isinstance(result, str)


def test_derived_session_uuid_is_deterministic(tmp_path):
    a = derived_session_uuid(AgentRole.IMPLEMENTER, tmp_path)
    b = derived_session_uuid(AgentRole.IMPLEMENTER, tmp_path)
    assert a == b


def test_derived_session_uuid_differs_by_role(tmp_path):
    impl = derived_session_uuid(AgentRole.IMPLEMENTER, tmp_path)
    rev = derived_session_uuid(AgentRole.REVIEWER, tmp_path)
    assert impl != rev


def test_derived_session_uuid_differs_by_worktree_path(tmp_path):
    path_a = tmp_path / "issue-1"
    path_b = tmp_path / "issue-2"
    a = derived_session_uuid(AgentRole.IMPLEMENTER, path_a)
    b = derived_session_uuid(AgentRole.IMPLEMENTER, path_b)
    assert a != b


def test_derived_session_uuid_is_valid_uuid(tmp_path):
    result = derived_session_uuid(AgentRole.IMPLEMENTER, tmp_path)
    parsed = uuid.UUID(result)
    assert str(parsed) == result


def test_derived_session_uuid_uses_resolved_path(tmp_path):
    direct = derived_session_uuid(AgentRole.IMPLEMENTER, tmp_path)
    via_resolved = derived_session_uuid(AgentRole.IMPLEMENTER, tmp_path.resolve())
    assert direct == via_resolved
