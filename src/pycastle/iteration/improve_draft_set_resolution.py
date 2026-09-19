from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycastle.iteration.improve_drafts import DraftSetValidationError, read_draft_set

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from pycastle.config import Config
    from pycastle.iteration.improve_drafts import IssueDraft

_MAX_CORRECTION_ATTEMPTS = 3


@dataclass(frozen=True)
class Ready:
    drafts: list[IssueDraft]


@dataclass(frozen=True)
class Unrepairable:
    problems: list[str]
    draft_files: dict[str, str]


DraftSetResolutionOutcome = Ready | Unrepairable


async def resolve_draft_set(
    *,
    draft_dir: Path,
    cfg: Config,
    correction_callback: Callable[[DraftSetValidationError, int], Awaitable[None]],
) -> DraftSetResolutionOutcome:
    last_exc: DraftSetValidationError | None = None
    drafts: list[IssueDraft] | None = None
    for attempt in range(_MAX_CORRECTION_ATTEMPTS + 1):
        try:
            drafts = read_draft_set(draft_dir, cfg)
            last_exc = None
            break
        except DraftSetValidationError as exc:
            last_exc = exc
            if attempt < _MAX_CORRECTION_ATTEMPTS:
                await correction_callback(exc, attempt)

    if last_exc is not None:
        draft_file_contents: dict[str, str] = {}
        if draft_dir.is_dir():  # noqa: ASYNC240
            for f in sorted(draft_dir.glob("*.md")):  # noqa: ASYNC240
                with contextlib.suppress(OSError):
                    draft_file_contents[f.name] = f.read_text(encoding="utf-8")
        return Unrepairable(problems=last_exc.problems, draft_files=draft_file_contents)

    # drafts is always set when last_exc is None (the loop assigns it on break)
    if drafts is None:
        return Ready(drafts=[])  # unreachable; satisfies type narrowing
    return Ready(drafts=drafts)
