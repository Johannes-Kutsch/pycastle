from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LabelSpec:
    config_field: str
    default_name: str
    description: str
    color: str
    prompt_placeholder: str | None = None


CANONICAL_LABEL_SPECS: tuple[LabelSpec, ...] = (
    LabelSpec(
        config_field="bug_label",
        default_name="bug",
        description="Something isn't working",
        color="d73a4a",
        prompt_placeholder="BUG_LABEL",
    ),
    LabelSpec(
        config_field="issue_label",
        default_name="ready-for-agent",
        description="Fully specified, ready for afk agent",
        color="0be348",
        prompt_placeholder="READY_FOR_AGENT_LABEL",
    ),
    LabelSpec(
        config_field="hitl_label",
        default_name="ready-for-human",
        description="Requires human implementation",
        color="5181b8",
        prompt_placeholder="READY_FOR_HUMAN_LABEL",
    ),
    LabelSpec(
        config_field="enhancement_label",
        default_name="enhancement",
        description="New feature or request",
        color="a2eeef",
        prompt_placeholder="ENHANCEMENT_LABEL",
    ),
    LabelSpec(
        config_field="needs_triage_label",
        default_name="needs-triage",
        description="Maintainer needs to evaluate this issue",
        color="fbca04",
        prompt_placeholder="NEEDS_TRIAGE_LABEL",
    ),
    LabelSpec(
        config_field="needs_info_label",
        default_name="needs-info",
        description="Waiting on reporter for more information",
        color="b03176",
        prompt_placeholder="NEEDS_INFO_LABEL",
    ),
    LabelSpec(
        config_field="wontfix_label",
        default_name="wontfix",
        description="Will not be actioned",
        color="ffffff",
        prompt_placeholder="WONTFIX_LABEL",
    ),
    LabelSpec(
        config_field="refactor_slice_label",
        default_name="refactor-slice",
        description="Implementation scope: structural refactor only",
        color="0be348",
        prompt_placeholder="REFACTOR_SLICE_LABEL",
    ),
    LabelSpec(
        config_field="behavior_slice_label",
        default_name="behavior-slice",
        description="Implementation scope: observable behavior change",
        color="0be348",
        prompt_placeholder="BEHAVIOR_SLICE_LABEL",
    ),
    LabelSpec(
        config_field="docs_slice_label",
        default_name="docs-slice",
        description="Implementation scope: documentation only",
        color="0be348",
        prompt_placeholder="DOCS_SLICE_LABEL",
    ),
    LabelSpec(
        config_field="needs_slice_type_label",
        default_name="needs-slice-type",
        description="ready-for-agent issue missing exactly one slice-mode label",
        color="d73a4a",
    ),
)

CANONICAL_LABEL_DEFAULTS: dict[str, str] = {
    spec.config_field: spec.default_name for spec in CANONICAL_LABEL_SPECS
}

PROMPT_GLOBAL_LABEL_SPECS: tuple[LabelSpec, ...] = tuple(
    spec for spec in CANONICAL_LABEL_SPECS if spec.prompt_placeholder is not None
)
