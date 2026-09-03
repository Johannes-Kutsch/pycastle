"""Shared test-fixture helpers for improve role-session seeding and draft writing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import AgentRole, ScanCandidateItem
from pycastle.iteration.improve_role_session_store import (
    CandidateItem,
    CandidateList,
    CandidateRecord,
    ImproveRoleSessionStore,
)
from pycastle.session import RoleSession
from pycastle.session.role import SESSION_DIR_NAME

if TYPE_CHECKING:
    from pathlib import Path

_VALID_BODY = "A" * 120
_STATE_LABEL = "ready-for-agent"
_DRAFTS_SUBDIR = "_drafts"
_PYPROJECT_CONTENT = "[project]\nname='t'\n"


def _seed_worktree_project_files(role_session_dir: Path) -> None:
    """Write pyproject.toml to the worktree root when role_session_dir has the expected structure.

    Needed so that worktrees seeded directly (bypassing the fake create-worktree path)
    satisfy the worktree contents check, which requires pyproject.toml or requirements.txt.
    """
    if role_session_dir.parent.name != SESSION_DIR_NAME:
        return
    worktree_root = role_session_dir.parent.parent
    if (
        not (worktree_root / "pyproject.toml").exists()
        and not (worktree_root / "requirements.txt").exists()
    ):
        (worktree_root / "pyproject.toml").write_text(_PYPROJECT_CONTENT)


def _draft_dir(role_session_dir: Path) -> Path:
    return role_session_dir / _DRAFTS_SUBDIR


def _write_spec_draft(draft_dir: Path, *, body: str = _VALID_BODY) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "spec.md").write_text(
        f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n  - {_STATE_LABEL}\n---\n\n{body}"
    )


def _write_slice_draft(draft_dir: Path, name: str, *, body: str = _VALID_BODY) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / f"{name}.md").write_text(
        f"---\ntitle: {name} Slice\nlabels:\n  - behavior-slice\n  - {_STATE_LABEL}\n---\n\n{body}"
    )


def _seed_candidate_list(
    role_session_dir: Path,
    candidates: list[CandidateItem | ScanCandidateItem],
    *,
    no_candidate: bool = False,
    cursor: int = 0,
    fingerprint: str | None = "abc123",
) -> None:
    role_session_dir.mkdir(parents=True, exist_ok=True)
    _seed_worktree_project_files(role_session_dir)
    normalised = tuple(
        c if isinstance(c, CandidateItem) else CandidateItem(rank=c.rank, title=c.title)
        for c in candidates
    )
    store = ImproveRoleSessionStore(role_session_dir)
    store.write_candidate_list(
        CandidateList(candidates=normalised, no_candidate=no_candidate)
    )
    store.write_cursor(cursor)
    if fingerprint is not None:
        RoleSession(
            role_session_dir.parent.parent, AgentRole.IMPROVE
        ).write_fingerprint(fingerprint)


def _seed_candidate_record(
    role_session_dir: Path,
    idx: int,
    *,
    spec_number: int | None = None,
    labels_applied: bool = False,
) -> None:
    role_session_dir.mkdir(parents=True, exist_ok=True)
    store = ImproveRoleSessionStore(role_session_dir)
    record = CandidateRecord(
        spec_number=spec_number,
        spec_database_id=42 if spec_number is not None else None,
        spec_title="Seeded" if spec_number is not None else "",
        filed_tickets=(),
        labels_applied=labels_applied,
    )
    store.write_candidate_record(idx, record)


def _write_malformed_candidate_list(role_session_dir: Path, content: str) -> None:
    """Write raw (unparseable) text to the candidate list file for edge-case tests."""
    role_session_dir.mkdir(parents=True, exist_ok=True)
    _seed_worktree_project_files(role_session_dir)
    (role_session_dir / "_candidate_list").write_text(content, encoding="utf-8")


def _overwrite_candidate_cursor_raw(role_session_dir: Path, content: str) -> None:
    """Overwrite the cursor file with raw content for whitespace-parsing edge-case tests."""
    (role_session_dir / "_candidate_cursor").write_text(content, encoding="utf-8")


def _make_filing_github_svc() -> MagicMock:
    github_svc = MagicMock()
    github_svc.repo = "test/repo"
    github_svc.create_issue_in.side_effect = [
        (100, 1000),
        (101, 1001),
        (102, 1002),
    ]
    return github_svc
