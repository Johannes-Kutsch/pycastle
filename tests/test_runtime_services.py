from pathlib import Path
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import ProviderSessionStateRequest
from pycastle.services.runtime_services import CodexService


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
