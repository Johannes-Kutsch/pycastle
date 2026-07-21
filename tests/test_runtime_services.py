from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import ProviderSessionStateRequest
from pycastle.services.runtime_services import (
    ClaudeService,
    CodexService,
    OpenCodeService,
)


_FAR = datetime(2099, 1, 1, tzinfo=timezone.utc).astimezone()
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).astimezone()


def _role_session_mock() -> MagicMock:
    m = MagicMock()
    m.service_session_metadata.return_value = None
    m.exact_transcript_service_name.return_value = None
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


# --- Model-aware availability checks on AgentService implementations ---


def test_claude_service_is_available_for_model_false_when_model_restricted():
    svc = ClaudeService(accounts=[("account 1", "tok-1")])
    svc.build_env()  # picks tok-1 as active slot
    svc.mark_model_restricted("sonnet")
    assert svc.is_available(model="sonnet", now=_NOW) is False


def test_claude_service_is_available_for_model_true_for_unrestricted_model():
    svc = ClaudeService(accounts=[("account 1", "tok-1")])
    svc.build_env()
    svc.mark_model_restricted("sonnet")
    assert svc.is_available(model="haiku", now=_NOW) is True


def test_claude_service_model_restriction_does_not_affect_other_slots():
    svc = ClaudeService(accounts=[("account 1", "tok-1"), ("account 2", "tok-2")])
    svc.build_env()  # picks tok-1
    svc.mark_model_restricted("sonnet")
    assert svc.is_available(model="sonnet", now=_NOW) is True


def test_claude_service_model_restriction_persists_after_slot_rotation():
    svc = ClaudeService(accounts=[("account 1", "tok-1"), ("account 2", "tok-2")])
    svc.build_env()  # picks tok-1
    svc.mark_model_restricted("sonnet")
    svc.mark_permanently_exhausted()  # exhausts tok-1; pool rotates to tok-2
    svc.build_env()  # picks tok-2
    svc.mark_permanently_exhausted()  # exhausts tok-2
    # both slots exhausted — tok-1 also has sonnet restricted
    assert svc.is_available(model="sonnet", now=_NOW) is False


def test_claude_service_is_available_without_model_unaffected_by_model_restriction():
    svc = ClaudeService(accounts=[("account 1", "tok-1")])
    svc.build_env()
    svc.mark_model_restricted("sonnet")
    assert svc.is_available(now=_NOW) is True


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


def test_opencode_service_is_available_for_model_false_when_model_restricted():
    svc = OpenCodeService(api_key="tok-1")
    svc.build_env()
    svc.mark_model_restricted("kimi-k2.6")
    assert svc.is_available(model="kimi-k2.6", now=_NOW) is False


def test_opencode_service_is_available_for_model_true_for_unrestricted_model():
    svc = OpenCodeService(api_key="tok-1")
    svc.build_env()
    svc.mark_model_restricted("kimi-k2.6")
    assert svc.is_available(model="deepseek-v4-flash", now=_NOW) is True


def test_opencode_service_is_available_without_model_unaffected_by_model_restriction():
    svc = OpenCodeService(api_key="tok-1")
    svc.build_env()
    svc.mark_model_restricted("kimi-k2.6")
    assert svc.is_available(now=_NOW) is True


def test_codex_service_model_restriction_persists_after_temporary_exhaustion_and_wake():
    past_reset = datetime(2025, 6, 1, tzinfo=timezone.utc).astimezone()
    svc = CodexService()
    svc.mark_model_restricted("gpt-5.5")
    svc.mark_exhausted(past_reset)  # wake time ~2025-06-01T00:02 UTC, before _NOW
    assert svc.is_available(model="gpt-5.5", now=_NOW) is False
    assert svc.is_available(model="gpt-5.4", now=_NOW) is True


# --- build_env raises UsageLimitError when the credential pool is exhausted ---

from pycastle.errors import UsageLimitError


def test_claude_service_build_env_raises_usage_limit_error_when_pool_temporarily_exhausted():
    # Regression: pool exhaustion previously propagated as RuntimeError, causing the
    # orchestrator to loop endlessly instead of sleeping until accounts wake up.
    future_reset = datetime(2099, 1, 1, tzinfo=timezone.utc)
    svc = ClaudeService(accounts=[("account 1", "tok-1")])
    svc.build_env()  # picks tok-1
    svc.mark_exhausted(future_reset)  # tok-1 exhausted with a finite wake time

    import pytest
    with pytest.raises(UsageLimitError) as exc_info:
        svc.build_env()

    assert exc_info.value.is_permanent is False
    assert exc_info.value.reset_time is not None
    assert exc_info.value.provider == "claude"


def test_claude_service_build_env_raises_permanent_usage_limit_error_when_pool_permanently_exhausted():
    svc = ClaudeService(accounts=[("account 1", "tok-1")])
    svc.build_env()  # picks tok-1
    svc.mark_permanently_exhausted()  # tok-1 permanently exhausted

    import pytest
    with pytest.raises(UsageLimitError) as exc_info:
        svc.build_env()

    assert exc_info.value.is_permanent is True
    assert exc_info.value.provider == "claude"
