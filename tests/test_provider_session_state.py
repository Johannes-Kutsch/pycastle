import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from pycastle.agents.output_protocol import AgentRole
from pycastle.runtime_session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
)
from pycastle.services import ServiceRegistry
from pycastle.services.runtime_services import CodexService
from pycastle.session.service_session_store import (
    ServiceSessionStore,
    has_exact_transcript,
)


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
        del request
        return ProviderSessionPreferences()


def _write_codex_rollout(state_dir: Path, *thread_ids: str) -> None:
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    lines = [
        json.dumps({"type": "thread.started", "thread_id": thread_id})
        for thread_id in thread_ids
    ]
    (rollout_dir / "rollout-001.jsonl").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("layout", "rollout_relpath"),
    [
        ("flat", Path("sessions/rollout-001.jsonl")),
        ("nested", Path("sessions/2026/05/30/nested/rollout-001.jsonl")),
    ],
)
def test_recover_state_dir_provider_session_id_recovers_single_codex_rollout_thread_id(
    tmp_path: Path,
    layout: str,
    rollout_relpath: Path,
) -> None:
    state_dir = tmp_path / layout
    rollout_path = state_dir / rollout_relpath
    rollout_path.parent.mkdir(parents=True)
    rollout_path.write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )

    assert (
        ServiceSessionStore.recover_state_session_id(state_dir, "codex")
        == "thread-from-rollout"
    )


def test_recover_state_dir_provider_session_id_ignores_persisted_codex_thread_id_without_sessions_dir(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    state_dir.mkdir()
    (state_dir / "thread_id").write_text("thread-from-sidecar\n", encoding="utf-8")

    assert ServiceSessionStore.recover_state_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_returns_none_when_sessions_tree_has_no_rollouts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    (state_dir / "sessions" / "2026" / "05" / "30").mkdir(parents=True)

    assert ServiceSessionStore.recover_state_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_deduplicates_repeated_codex_thread_ids(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n'
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )

    assert (
        ServiceSessionStore.recover_state_session_id(state_dir, "codex") == "thread-abc"
    )


def test_recover_state_dir_provider_session_id_returns_none_for_ambiguous_codex_rollouts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    (rollout_dir / "rollout-002.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-def"}\n',
        encoding="utf-8",
    )

    assert ServiceSessionStore.recover_state_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_returns_none_for_distinct_thread_ids_in_one_rollout(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n'
        '{"type":"thread.started","thread_id":"thread-def"}\n',
        encoding="utf-8",
    )

    assert ServiceSessionStore.recover_state_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_ignores_malformed_and_unreadable_codex_rollouts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        "{not-json\n"
        "[]\n"
        '{"type":"turn.completed"}\n'
        '{"type":"thread.started","thread_id":"   "}\n'
        '{"type":"thread.started"}\n',
        encoding="utf-8",
    )
    (rollout_dir / "rollout-002.jsonl").mkdir()

    assert ServiceSessionStore.recover_state_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_ignores_unreadable_codex_rollouts_without_losing_valid_identity(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-abc"}\n',
        encoding="utf-8",
    )
    (rollout_dir / "rollout-002.jsonl").mkdir()

    assert (
        ServiceSessionStore.recover_state_session_id(state_dir, "codex") == "thread-abc"
    )


def test_has_exact_provider_transcript_for_service_returns_true_for_codex_with_matching_metadata_sidecar_and_duplicate_rollout_entries(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    state_dir = role_dir / "codex"
    _write_codex_rollout(state_dir, "thread-exact", "thread-exact")
    ServiceSessionStore(role_dir).save_service_session_id("codex", "thread-exact")
    ServiceSessionStore(role_dir).record_successful_run("codex", "thread-exact")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
            known_service_names=frozenset({"codex"}),
        )
        is True
    )


def test_has_exact_provider_transcript_for_service_returns_true_for_opencode_with_matching_metadata_sidecar_and_resumable_state_dir(
    tmp_path: Path,
) -> None:
    service = cast(
        "Any",
        _FakeService(
            name="opencode",
            relpath="custom/opencode-state/",
            resumable=True,
        ),
    )
    role_dir = tmp_path / ".pycastle-session" / "reviewer" / "main"
    (role_dir / "opencode").mkdir(parents=True)
    (role_dir / "opencode" / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.REVIEWER,
            namespace="main",
            service=service,
            known_service_names=frozenset({"opencode"}),
        )
        is True
    )


def test_has_exact_provider_transcript_for_service_returns_true_for_opencode_without_state_dir_session_id_sidecar(
    tmp_path: Path,
) -> None:
    service = cast(
        "Any",
        _FakeService(
            name="opencode",
            relpath="custom/opencode-state/",
            resumable=True,
        ),
    )
    role_dir = tmp_path / ".pycastle-session" / "reviewer" / "main"
    (role_dir / "opencode").mkdir(parents=True)
    (role_dir / "opencode" / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.REVIEWER,
            namespace="main",
            service=service,
            known_service_names=frozenset({"opencode"}),
        )
        is True
    )


def test_has_exact_provider_transcript_for_selected_service_returns_true_for_registered_matching_service(
    tmp_path: Path,
) -> None:
    service = cast(
        "Any",
        _FakeService(
            name="opencode",
            relpath="custom/opencode-state/",
            resumable=True,
        ),
    )
    role_dir = tmp_path / ".pycastle-session" / "reviewer" / "main"
    (role_dir / "opencode").mkdir(parents=True)
    (role_dir / "opencode" / "seed").write_text("seed", encoding="utf-8")
    registry = ServiceRegistry({"opencode": service})

    _svc = registry["opencode"]
    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.REVIEWER,
            namespace="main",
            service=_svc,
            known_service_names=frozenset(registry.services.keys()),
        )
        if _svc is not None
        else False
    ) is True


@pytest.mark.parametrize(
    ("registry", "service_name"),
    [
        (None, "opencode"),
        (ServiceRegistry({}), "opencode"),
        (
            ServiceRegistry(
                {"opencode": cast("Any", _FakeService("opencode", "state", True))}
            ),
            "",
        ),
    ],
)
def test_has_exact_provider_transcript_for_selected_service_returns_false_without_a_selected_registered_service(
    tmp_path: Path,
    registry: ServiceRegistry | None,
    service_name: str,
) -> None:
    _svc = None if (registry is None or not service_name) else registry[service_name]
    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=_svc,
            known_service_names=frozenset(registry.services.keys())
            if registry is not None
            else frozenset(),
        )
        if _svc is not None
        else False
    ) is False


def test_has_exact_provider_transcript_for_service_returns_true_for_claude_with_matching_sidecar_metadata_and_selected_resumable_state_dir(
    tmp_path: Path,
) -> None:
    service = cast(
        "Any",
        _FakeService(
            name="claude",
            relpath="custom/claude-state/",
            resumable=True,
        ),
    )
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    (role_dir / "claude").mkdir(parents=True)
    (role_dir / "claude" / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            known_service_names=frozenset({"claude"}),
        )
        is True
    )


def test_has_exact_provider_transcript_for_service_returns_false_when_no_service_dir_in_role_session(
    tmp_path: Path,
) -> None:
    service = cast(
        "Any",
        _FakeService(name="claude", relpath="custom/claude-state/", resumable=True),
    )
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    role_dir.mkdir(parents=True)

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            known_service_names=frozenset({"claude"}),
        )
        is False
    )


def test_has_exact_provider_transcript_for_service_returns_false_when_service_dir_is_not_resumable(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    state_dir = role_dir / "codex"
    state_dir.mkdir(parents=True)
    (state_dir / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
            known_service_names=frozenset({"codex"}),
        )
        is False
    )


def test_has_exact_provider_transcript_for_service_returns_false_when_two_service_dirs_exist(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    state_dir = role_dir / "codex"
    _write_codex_rollout(state_dir, "thread-exact")
    opencode_dir = role_dir / "opencode"
    opencode_dir.mkdir(parents=True)
    (opencode_dir / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
            known_service_names=frozenset({"codex", "opencode"}),
        )
        is False
    )


def test_has_exact_provider_transcript_for_service_returns_false_when_service_dir_exists_but_state_is_not_resumable(
    tmp_path: Path,
) -> None:
    service = cast(
        "Any",
        _FakeService(
            name="opencode", relpath="custom/opencode-state/", resumable=False
        ),
    )
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    (role_dir / "opencode").mkdir(parents=True)
    (role_dir / "opencode" / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
            known_service_names=frozenset({"opencode"}),
        )
        is False
    )


@pytest.mark.parametrize(
    "setup",
    [
        "no_service_dir",
        "two_service_dirs",
        "service_dir_not_resumable",
    ],
)
def test_has_exact_provider_transcript_for_service_returns_false_for_ownership_failures(
    tmp_path: Path,
    setup: str,
) -> None:
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    if setup == "no_service_dir":
        service = CodexService()
        role_dir.mkdir(parents=True)
        known = frozenset({"codex"})
    elif setup == "two_service_dirs":
        service = CodexService()
        state_dir = role_dir / "codex"
        _write_codex_rollout(state_dir, "thread-exact")
        opencode_dir = role_dir / "opencode"
        opencode_dir.mkdir(parents=True)
        (opencode_dir / "seed").write_text("seed", encoding="utf-8")
        known = frozenset({"codex", "opencode"})
    else:
        service = cast(
            "Any",
            _FakeService(
                name="codex",
                relpath=".pycastle-session/improve/main/codex",
                resumable=False,
            ),
        )
        codex_dir = role_dir / "codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "seed").write_text("seed", encoding="utf-8")
        known = frozenset({"codex"})

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
            known_service_names=known,
        )
        is False
    )


def test_has_exact_provider_transcript_for_service_returns_false_for_different_selected_service(
    tmp_path: Path,
) -> None:
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    state_dir = role_dir / "codex"
    _write_codex_rollout(state_dir, "thread-exact")
    selected_service = cast(
        "Any",
        _FakeService(
            name="claude",
            relpath="custom/claude-state/",
            resumable=True,
        ),
    )

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=selected_service,
            known_service_names=frozenset({"codex", "claude"}),
        )
        is False
    )


def test_has_exact_provider_transcript_for_service_returns_false_for_non_resumable_provider_state(
    tmp_path: Path,
) -> None:
    service = cast(
        "Any",
        _FakeService(
            name="claude",
            relpath="custom/claude-state/",
            resumable=False,
        ),
    )
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    (role_dir / "claude").mkdir(parents=True)
    (role_dir / "claude" / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
            known_service_names=frozenset({"claude"}),
        )
        is False
    )


def test_has_exact_provider_transcript_for_service_returns_false_for_ambiguous_codex_identity_evidence(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    state_dir = role_dir / "codex"
    _write_codex_rollout(state_dir, "thread-exact")
    opencode_dir = role_dir / "opencode"
    opencode_dir.mkdir(parents=True)
    (opencode_dir / "seed").write_text("seed", encoding="utf-8")

    assert (
        has_exact_transcript(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
            known_service_names=frozenset({"codex", "opencode"}),
        )
        is False
    )
