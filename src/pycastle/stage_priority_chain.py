from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .config.types import StageOverride


@dataclass(frozen=True)
class ChainEntry:
    service: str
    model: str
    effort: str
    fallback: StageOverride | None


@dataclass(frozen=True)
class ConfiguredCandidateSelection:
    has_configured_candidate: bool
    selected_chain: StageOverride | None


@dataclass(frozen=True)
class ConfiguredCandidateChain:
    candidates: tuple[StageOverride, ...]

    @property
    def has_configured_candidate(self) -> bool:
        return bool(self.candidates)


@dataclass(frozen=True)
class StageOverrideChain:
    override: StageOverride
    stage_name: str = ""
    configured_service_names: tuple[str, ...] = ()
    available_service_names: tuple[str, ...] = ()
    entries: tuple[ChainEntry, ...] = field(init=False)
    validation_labels: tuple[str, ...] = field(init=False)
    rendered_chain_label: str = field(init=False)
    referenced_service_names: tuple[str, ...] = field(init=False)
    configured_candidates: ConfiguredCandidateChain = field(init=False)
    configured_candidate_selection: ConfiguredCandidateSelection = field(init=False)

    def __post_init__(self) -> None:
        chain_nodes = _chain_nodes(self.override)
        entries = tuple(
            ChainEntry(
                service=node.service,
                model=node.model,
                effort=node.effort,
                fallback=node.fallback,
            )
            for node in chain_nodes
        )
        configured_services = set(self.configured_service_names)
        available_services = set(self.available_service_names)
        configured_candidates = _configured_candidates_from_chain_nodes(
            chain_nodes,
            configured_services,
        )
        selection = _select_configured_candidate_chain(
            configured_candidates=configured_candidates,
            configured_services=configured_services,
            available_services=available_services,
        )
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "validation_labels",
            tuple(
                self.stage_name if index == 0 else f"{self.stage_name} fallback"
                for index, _entry in enumerate(entries)
            ),
        )
        object.__setattr__(
            self,
            "rendered_chain_label",
            " -> ".join(
                entry.service if entry.service else "<missing>" for entry in entries
            ),
        )
        object.__setattr__(
            self,
            "referenced_service_names",
            tuple(_deduplicated_service_names(chain_nodes)),
        )
        object.__setattr__(
            self,
            "configured_candidates",
            ConfiguredCandidateChain(candidates=configured_candidates),
        )
        object.__setattr__(
            self,
            "configured_candidate_selection",
            selection,
        )

    @property
    def has_configured_candidate(self) -> bool:
        return self.configured_candidates.has_configured_candidate

    @property
    def chain_label(self) -> str:
        return self.rendered_chain_label

    @property
    def selected_chain(self) -> StageOverride | None:
        return self.configured_candidate_selection.selected_chain


def iter_stage_chain(override: StageOverride) -> Iterator[StageOverride]:
    node: StageOverride | None = override
    while node is not None:
        yield node
        node = node.fallback


def chain_entries(override: StageOverride) -> tuple[ChainEntry, ...]:
    return StageOverrideChain(override=override).entries


def validation_labels(stage_name: str, override: StageOverride) -> tuple[str, ...]:
    return StageOverrideChain(
        override=override,
        stage_name=stage_name,
    ).validation_labels


def render_chain_label(override: StageOverride) -> str:
    return StageOverrideChain(override=override).rendered_chain_label


def referenced_service_names(override: StageOverride) -> tuple[str, ...]:
    return StageOverrideChain(override=override).referenced_service_names


def configured_candidate_chain(
    override: StageOverride, *, configured_service_names: tuple[str, ...]
) -> ConfiguredCandidateChain:
    return StageOverrideChain(
        override=override,
        configured_service_names=configured_service_names,
    ).configured_candidates


def _build_chain(nodes: tuple[StageOverride, ...]) -> StageOverride | None:
    chain: StageOverride | None = None
    for node in reversed(nodes):
        chain = StageOverride(
            service=node.service,
            model=node.model,
            effort=node.effort,
            fallback=chain,
        )
    return chain


def _remaining_chain_is_fully_configured(
    override: StageOverride,
    configured_services: set[str],
) -> bool:
    return all(
        node.service in configured_services for node in iter_stage_chain(override)
    )


def _select_configured_candidate_chain(
    *,
    configured_candidates: tuple[StageOverride, ...],
    configured_services: set[str],
    available_services: set[str],
) -> ConfiguredCandidateSelection:
    if not configured_candidates:
        return ConfiguredCandidateSelection(
            has_configured_candidate=False,
            selected_chain=None,
        )
    for index, node in enumerate(configured_candidates):
        if node.service not in available_services:
            continue
        if _remaining_chain_is_fully_configured(node, configured_services):
            return ConfiguredCandidateSelection(
                has_configured_candidate=True,
                selected_chain=node,
            )
        return ConfiguredCandidateSelection(
            has_configured_candidate=True,
            selected_chain=_build_chain(configured_candidates[index:]),
        )
    first_configured = configured_candidates[0]
    if _remaining_chain_is_fully_configured(first_configured, configured_services):
        return ConfiguredCandidateSelection(
            has_configured_candidate=True,
            selected_chain=first_configured,
        )
    return ConfiguredCandidateSelection(
        has_configured_candidate=True,
        selected_chain=_build_chain(configured_candidates),
    )


def select_configured_candidate_chain(
    override: StageOverride,
    *,
    configured_service_names: tuple[str, ...],
    available_service_names: tuple[str, ...],
) -> ConfiguredCandidateSelection:
    chain = StageOverrideChain(
        override=override,
        configured_service_names=configured_service_names,
        available_service_names=available_service_names,
    )
    return chain.configured_candidate_selection


def _chain_nodes(override: StageOverride) -> tuple[StageOverride, ...]:
    return tuple(iter_stage_chain(override))


def _configured_candidates_from_chain_nodes(
    chain_nodes: tuple[StageOverride, ...],
    configured_services: set[str],
) -> tuple[StageOverride, ...]:
    return tuple(node for node in chain_nodes if node.service in configured_services)


def _deduplicated_service_names(
    chain_nodes: tuple[StageOverride, ...],
) -> Iterator[str]:
    seen: set[str] = set()
    for node in chain_nodes:
        service = node.service.strip()
        if not service or service in seen:
            continue
        seen.add(service)
        yield service


__all__ = [
    "ChainEntry",
    "StageOverrideChain",
    "ConfiguredCandidateChain",
    "ConfiguredCandidateSelection",
    "StageOverride",
    "chain_entries",
    "configured_candidate_chain",
    "iter_stage_chain",
    "referenced_service_names",
    "render_chain_label",
    "select_configured_candidate_chain",
    "validation_labels",
]
