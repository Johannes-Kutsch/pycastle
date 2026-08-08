from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from pycastle.issue_readiness import SliceMode
from pycastle.runtime import OneShotRunRequest, run_one_shot
from pycastle.services.runtime_services import ToolPolicy

if TYPE_CHECKING:
    from pycastle.config.types import StageOverride
    from pycastle.execution_contracts import (
        PromptRuntimeExecutionAdapter,
        WorktreeMount,
    )
    from pycastle.services.service_registry import ServiceRegistry

_MODE_MAP: dict[str, SliceMode] = {
    "behavior": SliceMode.BEHAVIOR,
    "refactor": SliceMode.REFACTOR,
    "docs": SliceMode.DOCS,
}

_FALLBACK_REASON = "Model output was malformed or did not name a recognised slice mode."

_CLASSIFIER_PROMPT_TEMPLATE = """\
You are a slice-mode classifier. Given an issue title and body, classify the \
issue into exactly one of three slice modes by inspecting the repository if needed.

## Slice-mode definitions

- **behavior-slice**: The issue introduces or changes observable behavior that \
can be verified by a new test. Acceptance criteria describe behavior and \
observable surface.
- **refactor-slice**: The issue's steps cannot be verified by a new test of \
observable behavior (symbol moves, renames, protocol introductions, import \
rewires, dead-code removal). No new observable behavior is introduced.
- **docs-slice**: The only artifacts are markdown edits (CONTEXT.md, ADRs, \
README). No code, no tests.

## Tie-break rules

- If the issue could be either refactor-slice or behavior-slice, choose \
**behavior-slice**.
- If the issue could be either docs-slice or a code-touching slice, output \
**uncertain** with a one-sentence reason.

## Issue to classify

**Title:** {title}

**Body:**
{body}

## Output format

Respond with a JSON object only — no other text. Use one of:
- `{{"mode": "behavior"}}` — for behavior-slice
- `{{"mode": "refactor"}}` — for refactor-slice
- `{{"mode": "docs"}}` — for docs-slice
- `{{"reason": "<one sentence>"}}` — when docs-vs-code ambiguity makes classification uncertain
"""


@dataclasses.dataclass(frozen=True)
class ConcreteSliceVerdict:
    mode: SliceMode


@dataclasses.dataclass(frozen=True)
class UncertainSliceVerdict:
    reason: str


type SliceClassifierVerdict = ConcreteSliceVerdict | UncertainSliceVerdict


def parse_classifier_output(raw: str) -> SliceClassifierVerdict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return UncertainSliceVerdict(reason=_FALLBACK_REASON)

    if not isinstance(data, dict):
        return UncertainSliceVerdict(reason=_FALLBACK_REASON)

    mode_key = data.get("mode")
    if isinstance(mode_key, str) and mode_key in _MODE_MAP:
        return ConcreteSliceVerdict(mode=_MODE_MAP[mode_key])

    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        return UncertainSliceVerdict(reason=reason.strip())

    return UncertainSliceVerdict(reason=_FALLBACK_REASON)


async def classify_slice(
    *,
    issue_title: str,
    issue_body: str,
    worktree: WorktreeMount,
    plan_override: StageOverride,
    runner: PromptRuntimeExecutionAdapter,
    service_registry: ServiceRegistry,
) -> SliceClassifierVerdict:
    prompt = _CLASSIFIER_PROMPT_TEMPLATE.format(
        title=issue_title,
        body=issue_body,
    )
    request = OneShotRunRequest(
        prompt=prompt,
        worktree=worktree,
        override=plan_override,
        tool_policy=ToolPolicy.RESTRICTED,
        name="Slice Classifier",
    )
    result = await run_one_shot(
        runner=runner,
        service_registry=service_registry,
        request=request,
    )
    return parse_classifier_output(result.raw_output)


__all__ = [
    "ConcreteSliceVerdict",
    "SliceClassifierVerdict",
    "UncertainSliceVerdict",
    "classify_slice",
    "parse_classifier_output",
]
