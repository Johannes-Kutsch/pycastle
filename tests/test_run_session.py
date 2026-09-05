from __future__ import annotations

from pathlib import Path

import pytest
from agent_runtime.errors import AgentCredentialFailureError

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import (
    ProviderSessionState,
    ProviderSessionStateRequest,
    RunKind,
)
from pycastle.services import ClaudeService
from pycastle.services.runtime_services import (
    AgentService,
    CodexService,
    OpenCodeService,
)
from pycastle.session import (
    RoleSession,
    provider_state_relpath,
)
from pycastle.session.role import session_uuid_for_role_session_path
from pycastle.session.run_session import (
    AuthSeedingRequirement,
    LocalAuthSeedAction,
)
from pycastle.session.service_session_store import (
    ServiceSessionStore,
    store_for_role_session,
)


def _role_session_session_uuid(role_session: object) -> str:
    role_session_path = getattr(role_session, "path", None)
    if isinstance(role_session_path, Path):
        identity_uuid = session_uuid_for_role_session_path(role_session_path)
        if identity_uuid is not None:
            return identity_uuid
    legacy = getattr(role_session, "session_uuid", None)
    if callable(legacy):
        return legacy()
    raise AssertionError("Unable to derive role session identifier")


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
            role_session=store_for_role_session(role_session),
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


def test_opencode_provider_session_state_returns_fresh_without_state_dir_session_id(
    tmp_path: Path,
) -> None:
    service = OpenCodeService()
    resume_identity = store_for_role_session(
        RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    )
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
    ServiceSessionStore(role_session.path).save_service_session_id(
        "codex", "thread-from-sidecar"
    )

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=store_for_role_session(role_session),
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )

    assert decision.run_kind is RunKind.RESUME
    assert decision.provider_session_id == "thread-from-sidecar"
    assert decision.state_dir_relpath == ".pycastle-session/implementer/codex/"
    assert decision.state_dir_path == state_dir
    assert decision.auth_seeding_requirement is AuthSeedingRequirement.REQUIRED
    action = decision.auth_seed_action
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
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n'
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )

    decision = service.provider_session_state(
        ProviderSessionStateRequest(
            role_session=store_for_role_session(role_session),
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )

    assert decision.run_kind is RunKind.RESUME
    assert decision.provider_session_id == "thread-from-rollout"
    assert (
        _role_session_service_session_id(role_session, "codex") == "thread-from-rollout"
    )
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
            role_session=store_for_role_session(role_session),
            provider_state_dir=state_dir,
            has_resumable_provider_state=True,
            state_dir_relpath=".pycastle-session/implementer/codex/",
        )
    )

    assert decision.run_kind is RunKind.FRESH
    assert decision.provider_session_id is None
    assert _role_session_service_session_id(role_session, "codex") is None
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
            role_session=store_for_role_session(
                RoleSession(tmp_path, AgentRole.IMPLEMENTER)
            ),
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


def test_local_auth_seed_action_applies_only_to_preserved_codex_provider_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from typing import cast

    from pycastle.services.runtime_services import AgentService

    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth","origin":"host"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    service = cast(
        "AgentService",
        type(
            "_FakeService",
            (),
            {
                "name": "codex",
                "state_dir_relpath": lambda self, role, namespace="": (
                    "custom/codex-state"
                ),
                "is_resumable": lambda self, state_dir: True,
            },
        )(),
    )

    plan_action = LocalAuthSeedAction(
        source=host_auth,
        destination=tmp_path / "custom" / "codex-state" / "auth.json",
    )

    plan_action.apply()

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

    provider_auth = (
        tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    )
    provider_auth.parent.mkdir(parents=True)
    provider_auth.write_text('{"mode":"oauth","origin":"provider"}', encoding="utf-8")

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

    assert exc_info.value.service_name == "codex"
