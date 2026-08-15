from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence


def startable_issues(
    issues: Sequence[dict],
    *,
    in_flight: Collection[int],
) -> list[dict]:
    return [
        issue
        for issue in issues
        if issue["number"] in in_flight or _open_blocker_count(issue) == 0
    ]


def _open_blocker_count(issue: dict) -> int:
    return int(issue.get("open_blockers_count") or 0)
