"""Tests for ImproveRoleSessionStore public interface."""

import json
from pathlib import Path

import pytest

from pycastle.iteration.improve_role_session_store import (
    CandidateItem,
    CandidateList,
    CandidateRecord,
    FiledSlice,
    ImproveRoleSessionStore,
)


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "role-session"


@pytest.fixture
def store(store_dir: Path) -> ImproveRoleSessionStore:
    return ImproveRoleSessionStore(store_dir)


# ── 1. Empty state — all reads return None ────────────────────────────────────


def test_empty_state_candidate_list_returns_none(
    store: ImproveRoleSessionStore,
) -> None:
    assert store.read_candidate_list() is None


def test_empty_state_cursor_returns_none(store: ImproveRoleSessionStore) -> None:
    assert store.read_cursor() is None


def test_empty_state_in_flight_returns_none(store: ImproveRoleSessionStore) -> None:
    assert store.read_in_flight() is None


def test_empty_state_candidate_record_returns_none(
    store: ImproveRoleSessionStore,
) -> None:
    assert store.read_candidate_record(0) is None


# ── 2. Candidate list round-trip ──────────────────────────────────────────────


def test_candidate_list_roundtrip_without_no_candidate(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    candidate_list = CandidateList(
        candidates=(
            CandidateItem(rank=1, title="Add caching layer"),
            CandidateItem(rank=2, title="Refactor auth"),
        ),
        no_candidate=False,
    )
    store.write_candidate_list(candidate_list)
    assert store.read_candidate_list() == candidate_list


def test_candidate_list_roundtrip_with_no_candidate(
    store: ImproveRoleSessionStore,
) -> None:
    candidate_list = CandidateList(
        candidates=(),
        no_candidate=True,
    )
    store.write_candidate_list(candidate_list)
    assert store.read_candidate_list() == candidate_list


def test_candidate_list_file_named_correctly(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_candidate_list(CandidateList(candidates=(), no_candidate=False))
    assert (store_dir / "_candidate_list").is_file()


def test_candidate_list_json_has_candidates_key(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_candidate_list(CandidateList(candidates=(), no_candidate=False))
    data = json.loads((store_dir / "_candidate_list").read_text(encoding="utf-8"))
    assert "candidates" in data


def test_candidate_list_json_has_no_candidate_key_when_true(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_candidate_list(CandidateList(candidates=(), no_candidate=True))
    data = json.loads((store_dir / "_candidate_list").read_text(encoding="utf-8"))
    assert data.get("no_candidate") is True


def test_candidate_list_json_omits_no_candidate_key_when_false(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_candidate_list(CandidateList(candidates=(), no_candidate=False))
    data = json.loads((store_dir / "_candidate_list").read_text(encoding="utf-8"))
    assert "no_candidate" not in data


# ── 3. Cursor round-trip ──────────────────────────────────────────────────────


def test_cursor_roundtrip(store: ImproveRoleSessionStore, store_dir: Path) -> None:
    store.write_cursor(7)
    assert store.read_cursor() == 7


def test_cursor_file_named_correctly(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_cursor(3)
    assert (store_dir / "_candidate_cursor").is_file()


def test_cursor_file_contains_string_form_of_integer(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_cursor(42)
    assert (store_dir / "_candidate_cursor").read_text(encoding="utf-8") == "42"


# ── 4. In-flight round-trip and clear ────────────────────────────────────────


def test_in_flight_roundtrip(store: ImproveRoleSessionStore) -> None:
    store.write_in_flight("02-prd.md")
    assert store.read_in_flight() == "02-prd.md"


def test_in_flight_clear_returns_none(store: ImproveRoleSessionStore) -> None:
    store.write_in_flight("01-scan.md")
    store.clear_in_flight()
    assert store.read_in_flight() is None


def test_in_flight_file_named_correctly(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_in_flight("03-issues.md")
    assert (store_dir / "_in_flight").is_file()


def test_in_flight_file_contains_phase_key(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.write_in_flight("01-scan.md")
    assert (store_dir / "_in_flight").read_text(encoding="utf-8") == "01-scan.md"


# ── 5. Candidate record round-trip ───────────────────────────────────────────


def test_candidate_record_roundtrip(store: ImproveRoleSessionStore) -> None:
    record = CandidateRecord(
        spec_number=101,
        spec_database_id=9001,
        spec_title="Improve caching",
        filed_slices=(
            FiledSlice(
                handle="slice-1", number=102, database_id=9002, title="Add Redis"
            ),
        ),
        labels_applied=True,
    )
    store.write_candidate_record(0, record)
    assert store.read_candidate_record(0) == record


def test_candidate_record_file_at_correct_path(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    record = CandidateRecord(
        spec_number=None,
        spec_database_id=None,
        spec_title="",
        filed_slices=(),
        labels_applied=False,
    )
    store.write_candidate_record(3, record)
    assert (store_dir / "candidates" / "3" / "_candidate_record").is_file()


def test_candidate_record_json_keys(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    record = CandidateRecord(
        spec_number=5,
        spec_database_id=55,
        spec_title="Title",
        filed_slices=(FiledSlice(handle="h", number=6, database_id=66, title="T"),),
        labels_applied=False,
    )
    store.write_candidate_record(0, record)
    data = json.loads(
        (store_dir / "candidates" / "0" / "_candidate_record").read_text(
            encoding="utf-8"
        )
    )
    assert "spec_number" in data
    assert "spec_database_id" in data
    assert "spec_title" in data
    assert "filed_slices" in data
    assert "labels_applied" in data
    assert data["filed_slices"][0].keys() >= {
        "handle",
        "number",
        "database_id",
        "title",
    }


# ── 6. Mark PRD completion ────────────────────────────────────────────────────


def test_mark_prd_completion_writes_empty_record_when_none(
    store: ImproveRoleSessionStore,
) -> None:
    store.mark_prd_completion(0)
    record = store.read_candidate_record(0)
    assert record is not None
    assert record.spec_number is None
    assert record.spec_database_id is None
    assert record.spec_title == ""
    assert record.filed_slices == ()
    assert record.labels_applied is False


def test_mark_prd_completion_leaves_existing_record_untouched(
    store: ImproveRoleSessionStore,
) -> None:
    existing = CandidateRecord(
        spec_number=77,
        spec_database_id=777,
        spec_title="Existing",
        filed_slices=(),
        labels_applied=True,
    )
    store.write_candidate_record(1, existing)
    store.mark_prd_completion(1)
    assert store.read_candidate_record(1) == existing


# ── 7. Tolerant reads ────────────────────────────────────────────────────────


def test_malformed_candidate_list_json_returns_none(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "_candidate_list").write_text("not-json", encoding="utf-8")
    assert store.read_candidate_list() is None


def test_malformed_candidate_record_json_returns_none(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    candidate_dir = store_dir / "candidates" / "0"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "_candidate_record").write_text("not-json", encoding="utf-8")
    assert store.read_candidate_record(0) is None


def test_non_integer_cursor_file_returns_none(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "_candidate_cursor").write_text("not-a-number", encoding="utf-8")
    assert store.read_cursor() is None


# ── 8. Auto-mkdir for nested candidate directories ────────────────────────────


def test_write_candidate_record_creates_parent_dirs(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    record = CandidateRecord(
        spec_number=None,
        spec_database_id=None,
        spec_title="",
        filed_slices=(),
        labels_applied=False,
    )
    store.write_candidate_record(5, record)
    assert (store_dir / "candidates" / "5").is_dir()


def test_mark_prd_completion_creates_parent_dirs(
    store: ImproveRoleSessionStore, store_dir: Path
) -> None:
    store.mark_prd_completion(9)
    assert (store_dir / "candidates" / "9").is_dir()
