from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_CANDIDATE_LIST_FILE = "_candidate_list"
_CANDIDATE_CURSOR_FILE = "_candidate_cursor"
_IN_FLIGHT_FILE = "_in_flight"
_CANDIDATE_RECORD_FILE = "_candidate_record"


@dataclass(frozen=True)
class CandidateItem:
    rank: int
    title: str


@dataclass(frozen=True)
class CandidateList:
    candidates: tuple[CandidateItem, ...]
    no_candidate: bool = False


@dataclass(frozen=True)
class FiledTicket:
    handle: str
    number: int
    database_id: int
    title: str


@dataclass(frozen=True)
class CandidateRecord:
    spec_number: int | None
    spec_database_id: int | None
    spec_title: str
    filed_tickets: tuple[FiledTicket, ...]
    labels_applied: bool


class ImproveRoleSessionStore:
    def __init__(self, role_session_dir: Path) -> None:
        self._dir = role_session_dir

    def _candidate_dir(self, idx: int) -> Path:
        return self._dir / "candidates" / str(idx)

    def read_candidate_list(self) -> CandidateList | None:
        path = self._dir / _CANDIDATE_LIST_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            candidates = tuple(
                CandidateItem(rank=c["rank"], title=c["title"])
                for c in data.get("candidates", [])
            )
            no_candidate = bool(data.get("no_candidate", False))
            return CandidateList(candidates=candidates, no_candidate=no_candidate)
        except (KeyError, json.JSONDecodeError):
            return None

    def write_candidate_list(self, candidate_list: CandidateList) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        data: dict = {
            "candidates": [
                {"rank": c.rank, "title": c.title} for c in candidate_list.candidates
            ],
        }
        if candidate_list.no_candidate:
            data["no_candidate"] = True
        (self._dir / _CANDIDATE_LIST_FILE).write_text(
            json.dumps(data), encoding="utf-8"
        )

    def read_cursor(self) -> int | None:
        path = self._dir / _CANDIDATE_CURSOR_FILE
        if not path.is_file():
            return None
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    def write_cursor(self, cursor: int) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / _CANDIDATE_CURSOR_FILE).write_text(str(cursor), encoding="utf-8")

    def read_in_flight(self) -> str | None:
        path = self._dir / _IN_FLIGHT_FILE
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text or None

    def write_in_flight(self, phase_key: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / _IN_FLIGHT_FILE).write_text(phase_key, encoding="utf-8")

    def clear_in_flight(self) -> None:
        (self._dir / _IN_FLIGHT_FILE).unlink(missing_ok=True)

    def read_candidate_record(self, idx: int) -> CandidateRecord | None:
        path = self._candidate_dir(idx) / _CANDIDATE_RECORD_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            filed_tickets = tuple(
                FiledTicket(
                    handle=s["handle"],
                    number=s["number"],
                    database_id=s["database_id"],
                    title=s["title"],
                )
                for s in data.get("filed_tickets", [])
            )
            return CandidateRecord(
                spec_number=data.get("spec_number"),
                spec_database_id=data.get("spec_database_id"),
                spec_title=data.get("spec_title", ""),
                filed_tickets=filed_tickets,
                labels_applied=bool(data.get("labels_applied", False)),
            )
        except (KeyError, json.JSONDecodeError):
            return None

    def write_candidate_record(self, idx: int, record: CandidateRecord) -> None:
        candidate_dir = self._candidate_dir(idx)
        candidate_dir.mkdir(parents=True, exist_ok=True)
        data: dict = {
            "spec_number": record.spec_number,
            "spec_database_id": record.spec_database_id,
            "spec_title": record.spec_title,
            "filed_tickets": [
                {
                    "handle": s.handle,
                    "number": s.number,
                    "database_id": s.database_id,
                    "title": s.title,
                }
                for s in record.filed_tickets
            ],
            "labels_applied": record.labels_applied,
        }
        (candidate_dir / _CANDIDATE_RECORD_FILE).write_text(
            json.dumps(data), encoding="utf-8"
        )

    def mark_spec_completion(self, idx: int) -> None:
        if self.read_candidate_record(idx) is None:
            self.write_candidate_record(
                idx,
                CandidateRecord(
                    spec_number=None,
                    spec_database_id=None,
                    spec_title="",
                    filed_tickets=(),
                    labels_applied=False,
                ),
            )
