from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.iteration.improve_drafts import IssueDraft

_CANDIDATE_RECORD_FILE = "_candidate_record"


class FilingPort(Protocol):
    def create_issue(
        self, title: str, body: str, labels: list[str]
    ) -> tuple[int, int]: ...

    def register_sub_issue(
        self, parent_number: int, child_database_id: int
    ) -> None: ...

    def add_issue_dependency(
        self, child_number: int, blocker_database_id: int
    ) -> None: ...

    def apply_label(self, issue_number: int, label: str) -> None: ...


@dataclasses.dataclass
class _FiledIssue:
    handle: str
    number: int
    database_id: int
    title: str


@dataclasses.dataclass
class _CandidateRecord:
    spec_number: int
    spec_database_id: int
    spec_title: str
    filed_slices: list[_FiledIssue]
    labels_applied: bool


def _load_record(role_dir: Path) -> _CandidateRecord | None:
    path = role_dir / _CANDIDATE_RECORD_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        filed_slices = [
            _FiledIssue(
                handle=s["handle"],
                number=s["number"],
                database_id=s["database_id"],
                title=s["title"],
            )
            for s in data.get("filed_slices", [])
        ]
        return _CandidateRecord(
            spec_number=data["spec_number"],
            spec_database_id=data["spec_database_id"],
            spec_title=data.get("spec_title", ""),
            filed_slices=filed_slices,
            labels_applied=data.get("labels_applied", False),
        )
    except (KeyError, json.JSONDecodeError):
        return None


def _save_record(role_dir: Path, record: _CandidateRecord) -> None:
    role_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "spec_number": record.spec_number,
        "spec_database_id": record.spec_database_id,
        "spec_title": record.spec_title,
        "filed_slices": [
            {
                "handle": s.handle,
                "number": s.number,
                "database_id": s.database_id,
                "title": s.title,
            }
            for s in record.filed_slices
        ],
        "labels_applied": record.labels_applied,
    }
    (role_dir / _CANDIDATE_RECORD_FILE).write_text(json.dumps(data), encoding="utf-8")


def _body_with_blockers(
    base_body: str,
    blocked_by: list[str],
    handle_to_filed: dict[str, _FiledIssue],
) -> str:
    if not blocked_by:
        return base_body
    refs = ", ".join(f"#{handle_to_filed[h].number}" for h in blocked_by)
    return base_body.rstrip() + f"\n\nBlocked by {refs}"


def _strip_state_label(labels: list[str], state_label: str) -> list[str]:
    return [lbl for lbl in labels if lbl != state_label]


def file_draft_set(
    drafts: list[IssueDraft],
    *,
    port: FilingPort,
    role_dir: Path,
    state_label: str,
) -> None:
    """File a validated draft set as a two-stage commit.

    Stage 1 creates every issue without the state label and wires all
    sub-issue and dependency edges.  Stage 2 applies the state label to
    every issue in the set.  A durable candidate record at *role_dir* makes
    both stages idempotent across resumed runs.
    """
    if not drafts:
        return

    spec_draft = drafts[0]
    slice_drafts = drafts[1:]

    record = _load_record(role_dir)
    handle_to_filed: dict[str, _FiledIssue] = {}

    if record is None:
        # Stage 1a: create the spec issue.
        spec_labels = _strip_state_label(spec_draft.labels, state_label)
        spec_number, spec_db_id = port.create_issue(
            spec_draft.title, spec_draft.body, spec_labels
        )
        spec_filed = _FiledIssue(
            handle=spec_draft.handle,
            number=spec_number,
            database_id=spec_db_id,
            title=spec_draft.title,
        )
        handle_to_filed[spec_draft.handle] = spec_filed
        record = _CandidateRecord(
            spec_number=spec_number,
            spec_database_id=spec_db_id,
            spec_title=spec_draft.title,
            filed_slices=[],
            labels_applied=False,
        )
        _save_record(role_dir, record)
    else:
        spec_filed = _FiledIssue(
            handle=spec_draft.handle,
            number=record.spec_number,
            database_id=record.spec_database_id,
            title=record.spec_title,
        )
        handle_to_filed[spec_draft.handle] = spec_filed
        for filed in record.filed_slices:
            handle_to_filed[filed.handle] = filed

    filed_handles = {s.handle for s in record.filed_slices}

    # Stage 1b: create each slice in order.
    for slice_draft in slice_drafts:
        if slice_draft.handle in filed_handles:
            continue

        slice_labels = _strip_state_label(slice_draft.labels, state_label)
        body = _body_with_blockers(
            slice_draft.body, slice_draft.blocked_by, handle_to_filed
        )
        slice_number, slice_db_id = port.create_issue(
            slice_draft.title, body, slice_labels
        )

        port.register_sub_issue(record.spec_number, slice_db_id)

        for blocker_handle in slice_draft.blocked_by:
            port.add_issue_dependency(
                slice_number, handle_to_filed[blocker_handle].database_id
            )

        slice_filed = _FiledIssue(
            handle=slice_draft.handle,
            number=slice_number,
            database_id=slice_db_id,
            title=slice_draft.title,
        )
        handle_to_filed[slice_draft.handle] = slice_filed
        record.filed_slices.append(slice_filed)
        _save_record(role_dir, record)

    # Stage 2: apply state label to every issue in the set.
    if not record.labels_applied:
        port.apply_label(record.spec_number, state_label)
        for filed in record.filed_slices:
            port.apply_label(filed.number, state_label)
        record.labels_applied = True
        _save_record(role_dir, record)
