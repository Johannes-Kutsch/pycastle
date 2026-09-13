from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from pycastle.agents.output_protocol import AgentRole

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pycastle.config.loader import Config
    from pycastle.config.types import StageOverride


@dataclasses.dataclass(frozen=True)
class Stage:
    stage_key: str
    config_attr: str
    roles: tuple[AgentRole, ...]


STAGES: tuple[Stage, ...] = (
    Stage("plan", "plan_override", (AgentRole.PLANNER,)),
    Stage("implement", "implement_override", (AgentRole.IMPLEMENTER,)),
    Stage("review", "review_override", (AgentRole.REVIEWER,)),
    Stage("merge", "merge_override", (AgentRole.MERGER,)),
    Stage(
        "preflight_issue",
        "preflight_issue_override",
        (AgentRole.PREFLIGHT_ISSUE, AgentRole.FAILURE_REPORT),
    ),
    Stage("improve", "improve_override", (AgentRole.IMPROVE,)),
)

_ROLE_TO_STAGE: dict[AgentRole, Stage] = {
    role: stage for stage in STAGES for role in stage.roles
}

_KEY_TO_STAGE: dict[str, Stage] = {stage.stage_key: stage for stage in STAGES}


def stage_for_role(role: AgentRole) -> Stage | None:
    return _ROLE_TO_STAGE.get(role)


def stage_key_for_role(role: AgentRole) -> str | None:
    stage = _ROLE_TO_STAGE.get(role)
    return stage.stage_key if stage is not None else None


def override_for_stage_key(cfg: Config, stage_key: str) -> StageOverride | None:
    stage = _KEY_TO_STAGE.get(stage_key)
    if stage is None:
        return None
    return getattr(cfg, stage.config_attr)


def iter_stage_overrides(cfg: Config) -> Iterable[tuple[str, StageOverride]]:
    for stage in STAGES:
        yield stage.stage_key, getattr(cfg, stage.config_attr)


__all__ = [
    "STAGES",
    "Stage",
    "iter_stage_overrides",
    "override_for_stage_key",
    "stage_for_role",
    "stage_key_for_role",
]
