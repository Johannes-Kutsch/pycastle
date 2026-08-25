from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Protocol, cast

from pycastle.iteration.improve_role_session_store import (
    CandidateRecord,
    FiledTicket,
    ImproveRoleSessionStore,
)

if TYPE_CHECKING:
    from pycastle.iteration.improve_drafts import IssueDraft
    from pycastle.services.github_service import GithubService


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

    def close_issue(self, issue_number: int) -> None: ...


class GithubFilingPort:
    def __init__(self, svc: GithubService) -> None:
        self._svc = svc

    def create_issue(self, title: str, body: str, labels: list[str]) -> tuple[int, int]:
        return self._svc.create_issue_in(self._svc.repo, title, body, labels)

    def register_sub_issue(self, parent_number: int, child_database_id: int) -> None:
        self._svc.add_sub_issue(parent_number, child_database_id)

    def add_issue_dependency(self, child_number: int, blocker_database_id: int) -> None:
        self._svc.add_issue_dependency(child_number, blocker_database_id)

    def apply_label(self, issue_number: int, label: str) -> None:
        self._svc.add_label_to_issue(issue_number, label)

    def close_issue(self, issue_number: int) -> None:
        self._svc.close_issue(issue_number)


def _parse_sections(body: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in body.split("\n"):
        if line.startswith("## "):
            sections.append((current_heading, "\n".join(current_lines)))
            current_heading = line
            current_lines = []
        else:
            current_lines.append(line)

    sections.append((current_heading, "\n".join(current_lines)))

    if sections and sections[0] == (None, ""):
        sections = sections[1:]

    return sections


def _reconstruct_sections(sections: list[tuple[str | None, str]]) -> str:
    parts: list[str] = []
    for heading, content in sections:
        stripped = content.strip()
        if heading is None:
            if stripped:
                parts.append(stripped)
        else:
            parts.append(heading + ("\n\n" + stripped if stripped else ""))
    return "\n\n".join(parts)


def _render_ticket_body(
    base_body: str,
    spec_number: int,
    blocked_by: list[str],
    handle_to_filed: dict[str, FiledTicket],
    extra_blocker_numbers: list[int] | None = None,
) -> str:
    intra = [f"#{handle_to_filed[h].number}" for h in blocked_by]
    extra = [f"#{n}" for n in (extra_blocker_numbers or [])]
    all_refs = intra + extra

    sections = _parse_sections(base_body)

    # Remove any existing ## Parent section.
    sections = [(h, c) for h, c in sections if h != "## Parent"]

    parent_section: tuple[str | None, str] = ("## Parent", f"#{spec_number}")
    what_idx = next(
        (i for i, (h, _) in enumerate(sections) if h == "## What to build"), -1
    )
    if what_idx >= 0:
        sections.insert(what_idx, parent_section)
    else:
        sections.insert(0, parent_section)

    blocked_content = (
        ", ".join(all_refs) if all_refs else "None — can start immediately."
    )
    blocked_section: tuple[str | None, str] = ("## Blocked by", blocked_content)

    # Remove any existing ## Blocked by section.
    sections = [(h, c) for h, c in sections if h != "## Blocked by"]

    ac_idx = next(
        (i for i, (h, _) in enumerate(sections) if h == "## Acceptance criteria"),
        -1,
    )
    if ac_idx >= 0:
        sections.insert(ac_idx + 1, blocked_section)
    else:
        files_idx = next(
            (i for i, (h, _) in enumerate(sections) if h and "Files touched" in h),
            -1,
        )
        if files_idx >= 0:
            sections.insert(files_idx, blocked_section)
        else:
            sections.append(blocked_section)

    return _reconstruct_sections(sections)


def _strip_state_label(labels: list[str], state_label: str) -> list[str]:
    return [lbl for lbl in labels if lbl != state_label]


def file_draft_set(
    drafts: list[IssueDraft],
    *,
    port: FilingPort,
    store: ImproveRoleSessionStore,
    candidate_idx: int,
    state_label: str,
    prev_spec: tuple[int, int] | None = None,
) -> None:
    """File a validated draft set as a two-stage commit.

    Stage 1 creates every issue without the state label and wires all
    sub-issue and dependency edges.  Stage 2 applies the state label to
    each ticket; the spec carries no state label because it is a tracking
    parent, not implementable work.  A durable candidate record in *store*
    at *candidate_idx* makes both stages idempotent across resumed runs.
    """
    if not drafts:
        return

    spec_draft = drafts[0]
    ticket_drafts = drafts[1:]

    record = store.read_candidate_record(candidate_idx)
    handle_to_filed: dict[str, FiledTicket] = {}

    if record is None or record.spec_number is None:
        # Stage 1a: create the spec issue.
        spec_labels = _strip_state_label(spec_draft.labels, state_label)
        spec_number, spec_db_id = port.create_issue(
            spec_draft.title, spec_draft.body, spec_labels
        )
        spec_filed = FiledTicket(
            handle=spec_draft.handle,
            number=spec_number,
            database_id=spec_db_id,
            title=spec_draft.title,
        )
        handle_to_filed[spec_draft.handle] = spec_filed
        record = CandidateRecord(
            spec_number=spec_number,
            spec_database_id=spec_db_id,
            spec_title=spec_draft.title,
            filed_tickets=(),
            labels_applied=False,
        )
        store.write_candidate_record(candidate_idx, record)
    else:
        # Branch condition: record.spec_number is not None (proved by if-guard above).
        spec_filed = FiledTicket(
            handle=spec_draft.handle,
            number=record.spec_number,
            database_id=cast("int", record.spec_database_id),
            title=record.spec_title,
        )
        handle_to_filed[spec_draft.handle] = spec_filed
        for filed in record.filed_tickets:
            handle_to_filed[filed.handle] = filed

    spec_number = cast("int", record.spec_number)
    filed_handles = {s.handle for s in record.filed_tickets}

    # Stage 1b: create each ticket in order.
    for ticket_draft in ticket_drafts:
        if ticket_draft.handle in filed_handles:
            continue

        ticket_labels = _strip_state_label(ticket_draft.labels, state_label)
        extra = [prev_spec[0]] if prev_spec is not None else []
        body = _render_ticket_body(
            ticket_draft.body,
            spec_number,
            ticket_draft.blocked_by,
            handle_to_filed,
            extra,
        )
        ticket_number, ticket_db_id = port.create_issue(
            ticket_draft.title, body, ticket_labels
        )

        port.register_sub_issue(spec_number, ticket_db_id)

        for blocker_handle in ticket_draft.blocked_by:
            port.add_issue_dependency(
                ticket_number, handle_to_filed[blocker_handle].database_id
            )
        if prev_spec is not None:
            port.add_issue_dependency(ticket_number, prev_spec[1])

        ticket_filed = FiledTicket(
            handle=ticket_draft.handle,
            number=ticket_number,
            database_id=ticket_db_id,
            title=ticket_draft.title,
        )
        handle_to_filed[ticket_draft.handle] = ticket_filed
        record = dataclasses.replace(
            record, filed_tickets=(*record.filed_tickets, ticket_filed)
        )
        store.write_candidate_record(candidate_idx, record)

    # Stage 2: apply state label to tickets only; the spec is a tracking
    # parent and must not carry the state label (ADR 0058).
    if not record.labels_applied:
        for filed in record.filed_tickets:
            port.apply_label(filed.number, state_label)
        record = dataclasses.replace(record, labels_applied=True)
        store.write_candidate_record(candidate_idx, record)
