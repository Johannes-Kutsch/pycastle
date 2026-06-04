from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator

from .config.types import StageOverride


@dataclass(frozen=True)
class ChainEntry:
    service: str
    model: str
    effort: str
    fallback: StageOverride | None


def iter_stage_chain(override: StageOverride) -> Iterator[StageOverride]:
    node: StageOverride | None = override
    while node is not None:
        yield node
        node = node.fallback


def chain_entries(override: StageOverride) -> tuple[ChainEntry, ...]:
    return tuple(
        ChainEntry(
            service=node.service,
            model=node.model,
            effort=node.effort,
            fallback=node.fallback,
        )
        for node in iter_stage_chain(override)
    )


def validation_labels(stage_name: str, override: StageOverride) -> tuple[str, ...]:
    return tuple(
        stage_name if index == 0 else f"{stage_name} fallback"
        for index, _entry in enumerate(chain_entries(override))
    )


def render_chain_label(override: StageOverride) -> str:
    return " -> ".join(
        entry.service if entry.service else "<missing>"
        for entry in chain_entries(override)
    )
