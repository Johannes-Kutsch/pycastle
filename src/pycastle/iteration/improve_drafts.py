from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

from pycastle.agents.output_protocol import AgentOutputProtocolError
from pycastle.issue_readiness import classify_issue_readiness

if TYPE_CHECKING:
    from pathlib import Path

    from pycastle.config import Config

_SLICE_RE = re.compile(r"^\d{2}-")


class DraftSetValidationError(AgentOutputProtocolError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"Draft set validation failed:\n{joined}")


@dataclasses.dataclass(frozen=True)
class IssueDraft:
    handle: str
    title: str
    labels: list[str]
    body: str
    blocked_by: list[str] = dataclasses.field(default_factory=list)


def read_draft_set(directory: Path, cfg: Config) -> list[IssueDraft]:
    md_files = sorted(directory.glob("*.md"))
    if not md_files:
        raise DraftSetValidationError(["No draft files found in directory."])

    ticket_files = [f for f in md_files if _SLICE_RE.match(f.stem)]
    spec_files = [f for f in md_files if not _SLICE_RE.match(f.stem)]

    ordered_files = sorted(spec_files, key=lambda f: f.stem) + sorted(
        ticket_files, key=lambda f: f.stem
    )

    handles = {f.stem for f in ordered_files}
    problems: list[str] = []
    drafts: list[IssueDraft] = []

    if not spec_files:
        problems.append("draft set has no spec draft")

    for path in ordered_files:
        draft, draft_problems = _parse_draft(path)
        problems.extend(draft_problems)
        if draft is not None:
            drafts.append(draft)

    for draft in drafts:
        problems.extend(
            f"{draft.handle}: blocked_by refers to unknown handle {ref!r}"
            for ref in draft.blocked_by
            if ref not in handles
        )

    for draft in drafts:
        if not _SLICE_RE.match(draft.handle):
            continue
        issue = {"labels": draft.labels, "body": draft.body}
        readiness = classify_issue_readiness(issue, cfg)
        if not readiness.is_ready:
            problems.append(
                f"{draft.handle}: body does not pass readiness classification"
            )

    if problems:
        raise DraftSetValidationError(problems)

    return drafts


def _parse_draft(path: Path) -> tuple[IssueDraft | None, list[str]]:
    handle = path.stem
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []

    fm, body = _split_frontmatter(text)

    title = fm.get("title")
    labels_raw = fm.get("labels")

    if not title:
        problems.append(f"{handle}: missing required field 'title'")

    if problems:
        return None, problems

    if not isinstance(title, str):
        return None, [f"{handle}: unexpected frontmatter type for title"]

    if labels_raw is None:
        labels: list[str] = []
    elif isinstance(labels_raw, list):
        labels = [str(lbl) for lbl in labels_raw]
    else:
        return None, [f"{handle}: unexpected frontmatter type for labels"]

    raw_blocked = fm.get("blocked_by")
    blocked_by: list[object] = raw_blocked if isinstance(raw_blocked, list) else []

    return (
        IssueDraft(
            handle=handle,
            title=title,
            labels=labels,
            body=body,
            blocked_by=[str(b) for b in blocked_by],
        ),
        [],
    )


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---"):
        return {}, text

    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]

    end = rest.find("\n---")
    if end == -1:
        return {}, text

    fm_text = rest[:end]
    body = rest[end + 4 :]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]

    return _parse_simple_yaml(fm_text), body.strip()


def _parse_simple_yaml(text: str) -> dict[str, object]:
    result: dict[str, object] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line or line.startswith("#"):
            i += 1
            continue

        m = re.match(r"^(\w+):\s*(.*)", line)
        if m is None:
            i += 1
            continue

        key = m.group(1)
        value_str = m.group(2).strip()

        if value_str.startswith("[") and value_str.endswith("]"):
            inner = value_str[1:-1]
            items = [s.strip().strip("'\"") for s in inner.split(",") if s.strip()]
            result[key] = items
        elif value_str == "":
            items = []
            i += 1
            while i < len(lines) and lines[i].startswith("  - "):
                items.append(lines[i][4:].strip())
                i += 1
            if items:
                result[key] = items
            continue
        else:
            result[key] = value_str

        i += 1

    return result
