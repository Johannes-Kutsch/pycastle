from __future__ import annotations

import enum
from dataclasses import dataclass, field

Section = tuple[str | None, str]


class OnMissing(enum.Enum):
    PREPEND = "prepend"
    APPEND = "append"
    RAISE = "raise"


class AnchorKind(enum.Enum):
    EXACT = "exact"
    SUBSTRING = "substring"


@dataclass(frozen=True)
class Anchor:
    text: str
    kind: AnchorKind = field(default=AnchorKind.EXACT)


def _parse_sections(body: str) -> list[Section]:
    sections: list[Section] = []
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


def _render_sections(sections: list[Section]) -> str:
    parts: list[str] = []
    for heading, content in sections:
        stripped = content.strip()
        if heading is None:
            if stripped:
                parts.append(stripped)
        else:
            parts.append(heading + ("\n\n" + stripped if stripped else ""))
    return "\n\n".join(parts)


def _heading_matches(anchor: Anchor, heading: str | None) -> bool:
    if heading is None:
        return False
    if anchor.kind == AnchorKind.EXACT:
        return heading == anchor.text
    return anchor.text in heading


def _normalize_anchors(anchors: list[Anchor | str]) -> list[Anchor]:
    return [a if isinstance(a, Anchor) else Anchor(text=a) for a in anchors]


class SectionedMarkdownBody:
    def __init__(self, body: str) -> None:
        self._sections: list[Section] = _parse_sections(body)

    def _find_anchor_index(self, anchors: list[Anchor]) -> int:
        for anchor in anchors:
            for i, (h, _) in enumerate(self._sections):
                if _heading_matches(anchor, h):
                    return i
        return -1

    def _remove_heading(self, heading: str) -> None:
        self._sections = [(h, c) for h, c in self._sections if h != heading]

    def _first_heading_index(self) -> int:
        return next((i for i, (h, _) in enumerate(self._sections) if h is not None), -1)

    def upsert_before(
        self,
        heading: str,
        content: str,
        anchors: list[Anchor | str],
        on_missing: OnMissing = OnMissing.PREPEND,
    ) -> SectionedMarkdownBody:
        normalized = _normalize_anchors(anchors)
        self._remove_heading(heading)
        anchor_idx = self._find_anchor_index(normalized)
        section: Section = (heading, content)

        if anchor_idx >= 0:
            self._sections.insert(anchor_idx, section)
        elif on_missing == OnMissing.PREPEND:
            first = self._first_heading_index()
            self._sections.insert(max(first, 0), section)
        elif on_missing == OnMissing.APPEND:
            self._sections.append(section)
        else:
            raise ValueError(f"No anchor found for {heading!r} and on_missing=RAISE")

        return self

    def upsert_after(
        self,
        heading: str,
        content: str,
        anchors: list[Anchor | str],
        on_missing: OnMissing = OnMissing.APPEND,
    ) -> SectionedMarkdownBody:
        normalized = _normalize_anchors(anchors)
        self._remove_heading(heading)
        anchor_idx = self._find_anchor_index(normalized)
        section: Section = (heading, content)

        if anchor_idx >= 0:
            self._sections.insert(anchor_idx + 1, section)
        elif on_missing == OnMissing.APPEND:
            self._sections.append(section)
        elif on_missing == OnMissing.PREPEND:
            first = self._first_heading_index()
            self._sections.insert(max(first, 0), section)
        else:
            raise ValueError(f"No anchor found for {heading!r} and on_missing=RAISE")

        return self

    def remove(self, heading: str) -> SectionedMarkdownBody:
        self._remove_heading(heading)
        return self

    def render(self) -> str:
        return _render_sections(self._sections)
