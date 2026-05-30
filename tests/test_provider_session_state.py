from pathlib import Path

import pytest

from pycastle.session.provider_session_state import (
    recover_state_dir_provider_session_id,
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
