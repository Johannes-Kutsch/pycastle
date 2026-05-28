from __future__ import annotations

from collections.abc import Iterator

from .config.types import StageOverride


def iter_stage_chain(override: StageOverride) -> Iterator[StageOverride]:
    node: StageOverride | None = override
    while node is not None:
        yield node
        node = node.fallback
