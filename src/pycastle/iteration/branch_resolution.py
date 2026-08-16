from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pycastle.config import Config


@dataclasses.dataclass(frozen=True)
class BranchFacts:
    dev_branch_on_origin: bool
    working_branch_on_local: bool
    working_branch_on_origin: bool


@dataclasses.dataclass(frozen=True)
class Fetch:
    pass


@dataclasses.dataclass(frozen=True)
class Seed:
    source: str
    target: str


@dataclasses.dataclass(frozen=True)
class PushUpstream:
    branch: str


type BranchSetupStep = Fetch | Seed | PushUpstream


@dataclasses.dataclass(frozen=True)
class BranchSetupPlan:
    steps: tuple[BranchSetupStep, ...]
    operating_branch: str


@dataclasses.dataclass(frozen=True)
class DevBranchMissing:
    dev_branch: str
    message: str


type BranchResolutionResult = BranchSetupPlan | DevBranchMissing


def resolve_branch_setup(cfg: Config, facts: BranchFacts) -> BranchResolutionResult:
    if not facts.dev_branch_on_origin:
        return DevBranchMissing(
            dev_branch=cfg.dev_branch,
            message=f"dev_branch '{cfg.dev_branch}' not found on origin",
        )

    if cfg.working_branch is None:
        return BranchSetupPlan(
            steps=(),
            operating_branch=cfg.dev_branch,
        )

    working_branch = cfg.working_branch
    branch_exists = facts.working_branch_on_local or facts.working_branch_on_origin

    if not branch_exists:
        steps: tuple[BranchSetupStep, ...] = (
            Fetch(),
            Seed(source=f"origin/{cfg.dev_branch}", target=working_branch),
            PushUpstream(branch=working_branch),
        )
    else:
        steps = ()

    return BranchSetupPlan(steps=steps, operating_branch=working_branch)
