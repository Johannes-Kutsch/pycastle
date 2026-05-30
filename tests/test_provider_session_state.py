import json
from pathlib import Path
from typing import Any, cast

import pytest
from dataclasses import dataclass

from pycastle.agents.output_protocol import AgentRole
from pycastle.services.codex_service import CodexService
from pycastle.session.provider_session_state import (
    has_exact_provider_transcript_for_service,
    recover_state_dir_provider_session_id,
    save_service_session_id,
    save_service_session_metadata,
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
        recover_state_dir_provider_session_id(state_dir, "codex")
        == "thread-from-rollout"
    )


def test_recover_state_dir_provider_session_id_ignores_persisted_codex_thread_id_without_sessions_dir(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    state_dir.mkdir()
    (state_dir / "thread_id").write_text("thread-from-sidecar\n", encoding="utf-8")

    assert recover_state_dir_provider_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_returns_none_when_sessions_tree_has_no_rollouts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    (state_dir / "sessions" / "2026" / "05" / "30").mkdir(parents=True)

    assert recover_state_dir_provider_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_deduplicates_repeated_codex_thread_ids(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-abc"}',
                '{"type":"thread.started","thread_id":"thread-abc"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert recover_state_dir_provider_session_id(state_dir, "codex") == "thread-abc"


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

    assert recover_state_dir_provider_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_returns_none_for_distinct_thread_ids_in_one_rollout(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-abc"}',
                '{"type":"thread.started","thread_id":"thread-def"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert recover_state_dir_provider_session_id(state_dir, "codex") is None


def test_recover_state_dir_provider_session_id_ignores_malformed_and_unreadable_codex_rollouts(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                "{not-json",
                "[]",
                '{"type":"turn.completed"}',
                '{"type":"thread.started","thread_id":"   "}',
                '{"type":"thread.started"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (rollout_dir / "rollout-002.jsonl").mkdir()

    assert recover_state_dir_provider_session_id(state_dir, "codex") is None


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

    assert recover_state_dir_provider_session_id(state_dir, "codex") == "thread-abc"


def test_has_exact_provider_transcript_for_service_returns_true_for_codex_with_matching_metadata_sidecar_and_duplicate_rollout_entries(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    state_dir = role_dir / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-exact"}',
                '{"type":"thread.started","thread_id":"thread-exact"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    save_service_session_id(role_dir, "codex", "thread-exact")
    save_service_session_metadata(role_dir, "codex", "thread-exact")

    assert (
        has_exact_provider_transcript_for_service(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
        )
        is True
    )


def test_has_exact_provider_transcript_for_service_returns_true_for_claude_with_matching_sidecar_metadata_and_selected_resumable_state_dir(
    tmp_path: Path,
) -> None:
    service = cast(
        Any,
        _FakeService(
            name="claude",
            relpath="custom/claude-state/",
            resumable=True,
        ),
    )
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    state_dir = tmp_path / "custom" / "claude-state"
    state_dir.mkdir(parents=True)
    (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    save_service_session_id(role_dir, "claude", "claude-session-uuid")
    save_service_session_metadata(role_dir, "claude", "claude-session-uuid")

    assert (
        has_exact_provider_transcript_for_service(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
        )
        is True
    )


def test_has_exact_provider_transcript_for_service_returns_false_when_metadata_payload_includes_another_provider(
    tmp_path: Path,
) -> None:
    service = CodexService()
    role_dir = tmp_path / ".pycastle-session" / "improve" / "main"
    state_dir = role_dir / "codex"
    rollout_dir = state_dir / "sessions" / "2026" / "05" / "30"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-exact"}\n',
        encoding="utf-8",
    )
    save_service_session_id(role_dir, "codex", "thread-exact")
    (role_dir / "_service_session_metadata.json").write_text(
        json.dumps(
            {
                "codex": {
                    "service": "codex",
                    "provider_session_id": "thread-exact",
                },
                "opencode": {
                    "service": "opencode",
                    "provider_session_id": "sess-other-provider",
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    assert (
        has_exact_provider_transcript_for_service(
            worktree=tmp_path,
            role=AgentRole.IMPROVE,
            namespace="main",
            service=service,
        )
        is False
    )
