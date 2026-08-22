from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AlreadyCurrent:
    pass


@dataclass(frozen=True)
class FastForwardLocalRef:
    pass


@dataclass(frozen=True)
class OperatingBranchDiverged:
    pass


@dataclass(frozen=True)
class NoUpstreamYet:
    pass


RefAncestry = Literal["equal", "local_ahead", "remote_ahead", "diverged"]
OperatingBranchRefRelation = (
    AlreadyCurrent | FastForwardLocalRef | OperatingBranchDiverged | NoUpstreamYet
)


def classify_ref_relation(
    *,
    upstream_ref_exists: bool,
    ancestry: RefAncestry | None,
) -> OperatingBranchRefRelation:
    if not upstream_ref_exists:
        return NoUpstreamYet()
    if ancestry in ("equal", "local_ahead"):
        return AlreadyCurrent()
    if ancestry == "remote_ahead":
        return FastForwardLocalRef()
    return OperatingBranchDiverged()
