from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import ProviderSessionStateRequest
from pycastle.services.runtime_services import (
    ClaudeService,
    CodexService,
    OpenCodeService,
    service_by_name,
)

_FAR = datetime(2099, 1, 1, tzinfo=UTC).astimezone()
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).astimezone()


def _role_session_mock() -> MagicMock:
    m = MagicMock()
    m.transcript_owner_service_name.return_value = None
    return m


def test_codex_state_dir_relpath_includes_codex_subdirectory():
    # Bug regression: state_dir_relpath was returning the role-only path (.pycastle-session/implementer)
    # rather than the service-specific subdirectory (.pycastle-session/implementer/codex/).
    # CODEX_HOME must point to the deeper path so auth.json is found at CODEX_HOME/auth.json.
    relpath = CodexService().state_dir_relpath(AgentRole.IMPLEMENTER)
    assert relpath == ".pycastle-session/implementer/codex/"


def test_codex_provider_session_state_populates_auth_seed_when_auth_json_absent(
    tmp_path: Path,
) -> None:
    # Bug regression: provider_session_state was never called in the ar execution path,
    # so auth_seed_action.apply() never ran and auth.json was never copied into the
    # container-accessible directory, causing Codex to return 401 Unauthorized.
    provider_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"

    state = CodexService().provider_session_state(
        ProviderSessionStateRequest(
            role_session=_role_session_mock(),
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=False,
        )
    )

    assert state.auth_seed_action is not None, (
        "auth_seed_action must be set when CODEX_HOME/auth.json is absent; "
        "omitting the apply() call causes 401 Unauthorized from Codex"
    )


def test_codex_provider_session_state_skips_auth_seed_when_auth_json_present(
    tmp_path: Path,
) -> None:
    provider_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    provider_state_dir.mkdir(parents=True)
    (provider_state_dir / "auth.json").write_text('{"token": "x"}', encoding="utf-8")

    state = CodexService().provider_session_state(
        ProviderSessionStateRequest(
            role_session=_role_session_mock(),
            provider_state_dir=provider_state_dir,
            has_resumable_provider_state=False,
        )
    )

    assert state.auth_seed_action is None


def test_codex_auth_seed_action_returns_action_when_auth_json_absent(
    tmp_path: Path,
) -> None:
    provider_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"

    action = CodexService().auth_seed_action(provider_state_dir)

    assert action is not None, (
        "auth_seed_action must return an action when auth.json is absent; "
        "the runner applies it to seed Codex credentials before the container starts"
    )


def test_codex_auth_seed_action_returns_none_when_auth_json_present(
    tmp_path: Path,
) -> None:
    provider_state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    provider_state_dir.mkdir(parents=True)
    (provider_state_dir / "auth.json").write_text('{"token": "x"}', encoding="utf-8")

    action = CodexService().auth_seed_action(provider_state_dir)

    assert action is None


def test_claude_service_auth_seed_action_returns_none(tmp_path: Path) -> None:
    assert ClaudeService().auth_seed_action(tmp_path) is None


# --- Model-aware availability checks on AgentService implementations ---


def test_codex_service_is_available_for_model_false_when_model_restricted():
    svc = CodexService()
    svc.mark_model_restricted("gpt-5.5")
    assert svc.is_available(model="gpt-5.5", now=_NOW) is False


def test_codex_service_is_available_for_model_true_for_unrestricted_model():
    svc = CodexService()
    svc.mark_model_restricted("gpt-5.5")
    assert svc.is_available(model="gpt-5.4", now=_NOW) is True


def test_codex_service_is_available_without_model_unaffected_by_model_restriction():
    svc = CodexService()
    svc.mark_model_restricted("gpt-5.5")
    assert svc.is_available(now=_NOW) is True


def test_codex_service_model_restriction_persists_after_temporary_exhaustion_and_wake():
    past_reset = datetime(2025, 6, 1, tzinfo=UTC).astimezone()
    svc = CodexService()
    svc.mark_model_restricted("gpt-5.5")
    svc.mark_exhausted(past_reset)  # wake time ~2025-06-01T00:02 UTC, before _NOW
    assert svc.is_available(model="gpt-5.5", now=_NOW) is False
    assert svc.is_available(model="gpt-5.4", now=_NOW) is True


def test_opencode_service_build_env_returns_env_normally_when_pool_has_available_credential():
    svc = OpenCodeService(accounts=[("account 1", "tok-1")])

    env = svc.build_env()

    assert env.get("OPENCODE_GO_API_KEY") == "tok-1"


# --- summary_line ---


def test_codex_service_summary_line_returns_local_auth_message():
    assert CodexService().summary_line() == "Codex auth: local auth available"


def test_opencode_service_summary_line_returns_api_key_message():
    assert (
        OpenCodeService(api_key="key").summary_line()
        == "OpenCode auth: API key configured"
    )


def test_claude_service_summary_line_returns_none_when_no_accounts():
    assert ClaudeService().summary_line() is None


def test_claude_service_summary_line_single_account():
    svc = ClaudeService(accounts=[("alice", "tok-1")])
    assert svc.summary_line() == "Claude accounts: alice (active)"


def test_claude_service_summary_line_two_accounts():
    svc = ClaudeService(accounts=[("alice", "tok-1"), ("bob", "tok-2")])
    assert svc.summary_line() == "Claude accounts: alice (active), bob (standby)"


def test_claude_service_summary_line_three_accounts():
    svc = ClaudeService(
        accounts=[("alice", "tok-1"), ("bob", "tok-2"), ("carol", "tok-3")]
    )
    assert (
        svc.summary_line()
        == "Claude accounts: alice (active), bob (standby), carol (standby)"
    )


# --- recover_provider_session_id / is_exact_resumable_provider_session ---


def test_claude_recover_provider_session_id_returns_none_for_none_dir() -> None:
    assert ClaudeService().recover_provider_session_id(None) is None


def test_claude_recover_provider_session_id_returns_none_for_any_dir(
    tmp_path: Path,
) -> None:
    assert ClaudeService().recover_provider_session_id(tmp_path) is None


def test_claude_is_exact_resumable_returns_false_when_both_none(
    tmp_path: Path,
) -> None:
    assert (
        ClaudeService().is_exact_resumable_provider_session(
            provider_session_id=None, provider_state_dir=None
        )
        is False
    )


def test_claude_is_exact_resumable_returns_false_when_session_id_none(
    tmp_path: Path,
) -> None:
    assert (
        ClaudeService().is_exact_resumable_provider_session(
            provider_session_id=None, provider_state_dir=tmp_path
        )
        is False
    )


def test_claude_is_exact_resumable_returns_false_when_state_dir_none() -> None:
    assert (
        ClaudeService().is_exact_resumable_provider_session(
            provider_session_id="sess-1", provider_state_dir=None
        )
        is False
    )


def test_claude_is_exact_resumable_returns_true_when_both_non_none(
    tmp_path: Path,
) -> None:
    assert (
        ClaudeService().is_exact_resumable_provider_session(
            provider_session_id="sess-1", provider_state_dir=tmp_path
        )
        is True
    )


def _write_codex_rollout(sessions_dir: Path, session_name: str, thread_id: str) -> None:
    rollout_dir = sessions_dir / session_name
    rollout_dir.mkdir(parents=True, exist_ok=True)
    (rollout_dir / "rollout-1.jsonl").write_text(
        f'{{"type": "thread.started", "thread_id": "{thread_id}"}}\n',
        encoding="utf-8",
    )


def test_codex_recover_provider_session_id_returns_none_for_none_dir() -> None:
    assert CodexService().recover_provider_session_id(None) is None


def test_codex_recover_provider_session_id_returns_none_when_sessions_dir_absent(
    tmp_path: Path,
) -> None:
    assert CodexService().recover_provider_session_id(tmp_path) is None


def test_codex_recover_provider_session_id_returns_none_when_no_rollout_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "sessions").mkdir()
    assert CodexService().recover_provider_session_id(tmp_path) is None


def test_codex_recover_provider_session_id_returns_thread_id_when_unique(
    tmp_path: Path,
) -> None:
    _write_codex_rollout(tmp_path / "sessions", "sess-abc", "thread-xyz")
    assert CodexService().recover_provider_session_id(tmp_path) == "thread-xyz"


def test_codex_recover_provider_session_id_returns_none_when_ambiguous(
    tmp_path: Path,
) -> None:
    _write_codex_rollout(tmp_path / "sessions", "sess-1", "thread-aaa")
    _write_codex_rollout(tmp_path / "sessions", "sess-2", "thread-bbb")
    assert CodexService().recover_provider_session_id(tmp_path) is None


def test_codex_is_exact_resumable_true_when_session_id_matches_recovered(
    tmp_path: Path,
) -> None:
    _write_codex_rollout(tmp_path / "sessions", "sess-abc", "thread-xyz")
    assert (
        CodexService().is_exact_resumable_provider_session(
            provider_session_id="thread-xyz", provider_state_dir=tmp_path
        )
        is True
    )


def test_codex_is_exact_resumable_false_when_session_id_differs(
    tmp_path: Path,
) -> None:
    _write_codex_rollout(tmp_path / "sessions", "sess-abc", "thread-xyz")
    assert (
        CodexService().is_exact_resumable_provider_session(
            provider_session_id="thread-other", provider_state_dir=tmp_path
        )
        is False
    )


def test_codex_is_exact_resumable_false_when_no_rollout(tmp_path: Path) -> None:
    assert (
        CodexService().is_exact_resumable_provider_session(
            provider_session_id="thread-xyz", provider_state_dir=tmp_path
        )
        is False
    )


def test_opencode_recover_provider_session_id_returns_none_for_none_dir() -> None:
    assert OpenCodeService().recover_provider_session_id(None) is None


def test_opencode_recover_provider_session_id_returns_none_when_sidecar_absent(
    tmp_path: Path,
) -> None:
    assert OpenCodeService().recover_provider_session_id(tmp_path) is None


def test_opencode_recover_provider_session_id_returns_sidecar_contents(
    tmp_path: Path,
) -> None:
    (tmp_path / "session_id").write_text("oc-sess-42", encoding="utf-8")
    assert OpenCodeService().recover_provider_session_id(tmp_path) == "oc-sess-42"


# --- service_by_name ---


def test_service_by_name_returns_claude_service() -> None:
    assert isinstance(service_by_name("claude"), ClaudeService)


def test_service_by_name_returns_codex_service() -> None:
    assert isinstance(service_by_name("codex"), CodexService)


def test_service_by_name_returns_opencode_service() -> None:
    assert isinstance(service_by_name("opencode"), OpenCodeService)


def test_service_by_name_unknown_recover_returns_none(tmp_path: Path) -> None:
    svc = service_by_name("unknown-service")
    assert svc.recover_provider_session_id(tmp_path) is None


def test_service_by_name_unknown_is_exact_resumable_false_when_session_id_none(
    tmp_path: Path,
) -> None:
    svc = service_by_name("unknown-service")
    assert (
        svc.is_exact_resumable_provider_session(
            provider_session_id=None, provider_state_dir=tmp_path
        )
        is False
    )


def test_service_by_name_unknown_is_exact_resumable_true_when_both_non_none(
    tmp_path: Path,
) -> None:
    svc = service_by_name("unknown-service")
    assert (
        svc.is_exact_resumable_provider_session(
            provider_session_id="any-id", provider_state_dir=tmp_path
        )
        is True
    )


# --- Pool-backed adapter: no-pool behavior (ClaudeService) ---


def test_claude_service_no_pool_is_available_returns_true() -> None:
    assert ClaudeService().is_available() is True


def test_claude_service_no_pool_is_available_with_model_returns_true() -> None:
    assert ClaudeService().is_available(model="sonnet") is True


def test_claude_service_no_pool_next_wake_time_raises() -> None:
    with pytest.raises(RuntimeError, match="no pool"):
        ClaudeService().next_wake_time()


def test_claude_service_no_pool_mark_exhausted_is_noop() -> None:
    ClaudeService().mark_exhausted(None)  # must not raise


def test_claude_service_no_pool_mark_permanently_exhausted_returns_none() -> None:
    assert ClaudeService().mark_permanently_exhausted() is None


def test_claude_service_no_pool_mark_model_restricted_is_noop() -> None:
    ClaudeService().mark_model_restricted("sonnet")  # must not raise


def test_claude_service_no_pool_account_names_returns_empty() -> None:
    assert ClaudeService().account_names() == []


# --- Pool-backed adapter: no-pool behavior (OpenCodeService) ---


def test_opencode_service_no_pool_is_available_returns_true() -> None:
    assert OpenCodeService().is_available() is True


def test_opencode_service_no_pool_is_available_with_model_returns_true() -> None:
    assert OpenCodeService().is_available(model="deepseek-v4-pro") is True


def test_opencode_service_no_pool_next_wake_time_raises() -> None:
    with pytest.raises(RuntimeError, match="no pool"):
        OpenCodeService().next_wake_time()


def test_opencode_service_no_pool_mark_exhausted_is_noop() -> None:
    OpenCodeService().mark_exhausted(None)  # must not raise


def test_opencode_service_no_pool_mark_permanently_exhausted_returns_none() -> None:
    assert OpenCodeService().mark_permanently_exhausted() is None


def test_opencode_service_no_pool_mark_model_restricted_is_noop() -> None:
    OpenCodeService().mark_model_restricted("deepseek-v4-pro")  # must not raise


def test_opencode_service_no_pool_account_names_returns_empty() -> None:
    assert OpenCodeService().account_names() == []


# --- Pool-backed adapter: with-pool behavior (ClaudeService) ---


def test_claude_service_with_pool_exhaustion_visible_through_next_wake_time() -> None:
    svc = ClaudeService(accounts=[("alice", "tok-1")])
    svc.build_env()  # pick_token sets _current_token on the helper
    svc.mark_exhausted(_FAR, _now=_NOW)
    assert svc.next_wake_time() > _NOW


def test_claude_service_with_pool_permanent_exhaustion_returns_label() -> None:
    svc = ClaudeService(accounts=[("alice", "tok-1")])
    svc.build_env()  # pick_token sets _current_token on the helper
    assert svc.mark_permanently_exhausted() == "alice"


def test_claude_service_with_pool_model_restriction_hides_model() -> None:
    svc = ClaudeService(accounts=[("alice", "tok-1")])
    svc.build_env()  # pick_token sets _current_token on the helper
    svc.mark_model_restricted("sonnet")
    assert svc.is_available(model="sonnet", now=_NOW) is False
    assert svc.is_available(model="opus", now=_NOW) is True


# --- Pool-backed adapter: with-pool behavior (OpenCodeService) ---


def test_opencode_service_with_pool_exhaustion_visible_through_next_wake_time() -> None:
    svc = OpenCodeService(accounts=[("alice", "tok-1")])
    # pick_token is called in __post_init__ for OpenCodeService
    svc.mark_exhausted(_FAR, _now=_NOW)
    assert svc.next_wake_time() > _NOW


def test_opencode_service_with_pool_permanent_exhaustion_returns_label() -> None:
    svc = OpenCodeService(accounts=[("alice", "tok-1")])
    assert svc.mark_permanently_exhausted() == "alice"


def test_opencode_service_with_pool_model_restriction_hides_model() -> None:
    svc = OpenCodeService(accounts=[("alice", "tok-1")])
    svc.mark_model_restricted("deepseek-v4-pro")
    assert svc.is_available(model="deepseek-v4-pro", now=_NOW) is False
    assert svc.is_available(model="deepseek-v4-flash", now=_NOW) is True
