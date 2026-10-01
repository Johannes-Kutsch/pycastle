"""Tests for improve_draft_correction_run module interface.

Covers: make_correction_callback factory — dispatched RunRequest shape
(name, prompt, work_body, mount_path, role, stage, status_display,
session_namespace, preserve_session_on_completion, model, effort, service).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pycastle.agents.output_protocol import AgentRole, CompletionOutput
from pycastle.config.types import StageOverride
from pycastle.iteration.improve_draft_correction_run import (
    CorrectionRunContext,
    make_correction_callback,
)
from pycastle.iteration.improve_drafts import DraftSetValidationError
from pycastle.prompts.dispatch import PromptKind
from pycastle.prompts.pipeline import PromptTemplate
from tests.support import FakeAgentRunner, RecordingStatusDisplay

if TYPE_CHECKING:
    from pathlib import Path


def _make_runner() -> FakeAgentRunner:
    return FakeAgentRunner(side_effect=lambda _req: CompletionOutput())


def _make_ctx(
    tmp_path: Path,
    runner: FakeAgentRunner,
    *,
    candidate_ordinal: int = 2,
    scan_set_size: int = 5,
    candidate_title: str = "My Feature",
    candidate_namespace: str = "candidate/1",
    improve_override: StageOverride | None = None,
) -> CorrectionRunContext:
    return CorrectionRunContext(
        candidate_ordinal=candidate_ordinal,
        scan_set_size=scan_set_size,
        candidate_title=candidate_title,
        candidate_namespace=candidate_namespace,
        sandbox_path=tmp_path,
        agent_runner=runner,
        improve_override=improve_override
        or StageOverride(model="claude-3", effort="high", service="anthropic"),
        status_display=RecordingStatusDisplay(),
    )


def _make_exc(problems: list[str] | None = None) -> DraftSetValidationError:
    return DraftSetValidationError(problems or ["body too short", "missing header"])


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AC1: single dispatch, RunRequest.name == "Draft Correction"
# ---------------------------------------------------------------------------


def test_callback_dispatches_exactly_one_run_named_draft_correction(
    tmp_path: Path,
) -> None:
    runner = _make_runner()
    ctx = _make_ctx(tmp_path, runner)
    cb = make_correction_callback(ctx)

    _run(cb(_make_exc(), 0, 3))

    assert len(runner.calls) == 1
    assert runner.calls[0].name == "Draft Correction"


# ---------------------------------------------------------------------------
# AC2: prompt uses IMPROVE_DRAFT_CORRECTION template, FOLLOW_UP kind,
#       VALIDATION_ERRORS == newline-joined exc.problems
# ---------------------------------------------------------------------------


def test_callback_prompt_uses_correction_template_follow_up_kind(
    tmp_path: Path,
) -> None:
    runner = _make_runner()
    ctx = _make_ctx(tmp_path, runner)
    exc = _make_exc(["first error", "second error"])
    cb = make_correction_callback(ctx)

    _run(cb(exc, 0, 3))

    prompt = runner.calls[0].prompt
    assert prompt.template == PromptTemplate.IMPROVE_DRAFT_CORRECTION
    assert prompt.kind == PromptKind.FOLLOW_UP
    assert prompt.scope_args["VALIDATION_ERRORS"] == "first error\nsecond error"


# ---------------------------------------------------------------------------
# AC3: work_body sentence format
# ---------------------------------------------------------------------------


def test_callback_work_body_sentence(tmp_path: Path) -> None:
    runner = _make_runner()
    ctx = _make_ctx(
        tmp_path,
        runner,
        candidate_ordinal=2,
        scan_set_size=5,
        candidate_title="My Feature",
    )
    cb = make_correction_callback(ctx)

    _run(cb(_make_exc(), 1, 4))

    expected = (
        'fixing draft validation errors for candidate 2/5 "My Feature" (attempt 2/4)'
    )
    assert runner.calls[0].work_body == expected


# ---------------------------------------------------------------------------
# AC4: mount_path, role, stage, status_display, session_namespace,
#       preserve_session_on_completion
# ---------------------------------------------------------------------------


def test_callback_run_request_structural_fields(tmp_path: Path) -> None:
    runner = _make_runner()
    status_display = RecordingStatusDisplay()
    ctx = CorrectionRunContext(
        candidate_ordinal=1,
        scan_set_size=3,
        candidate_title="Title",
        candidate_namespace="candidate/0",
        sandbox_path=tmp_path,
        agent_runner=runner,
        improve_override=StageOverride(),
        status_display=status_display,
    )
    cb = make_correction_callback(ctx)

    _run(cb(_make_exc(), 0, 2))

    req = runner.calls[0]
    assert req.mount_path == tmp_path
    assert req.role == AgentRole.IMPROVE
    assert req.stage == "improve-sandbox"
    assert req.status_display is status_display
    assert req.session_namespace == "candidate/0"
    assert req.preserve_session_on_completion is True


# ---------------------------------------------------------------------------
# AC5: model, effort, service come from improve_override
# ---------------------------------------------------------------------------


def test_callback_run_request_model_effort_service_from_override(
    tmp_path: Path,
) -> None:
    runner = _make_runner()
    ctx = _make_ctx(
        tmp_path,
        runner,
        improve_override=StageOverride(model="gpt-5", effort="max", service="openai"),
    )
    cb = make_correction_callback(ctx)

    _run(cb(_make_exc(), 0, 1))

    req = runner.calls[0]
    assert req.model == "gpt-5"
    assert req.effort == "max"
    assert req.service == "openai"


# ---------------------------------------------------------------------------
# AC6: distinct (attempt, total_attempts) pairs produce distinct work_body
#       strings; all other RunRequest fields are identical
# ---------------------------------------------------------------------------


def test_callback_distinct_attempt_pairs_produce_distinct_work_body(
    tmp_path: Path,
) -> None:
    runner = _make_runner()
    ctx = _make_ctx(
        tmp_path,
        runner,
        candidate_ordinal=3,
        scan_set_size=7,
        candidate_title="Edge Case",
    )
    exc = _make_exc(["err"])
    cb = make_correction_callback(ctx)

    _run(cb(exc, 0, 3))
    _run(cb(exc, 1, 3))
    _run(cb(exc, 2, 3))

    bodies = [r.work_body for r in runner.calls]
    assert bodies[0] == (
        'fixing draft validation errors for candidate 3/7 "Edge Case" (attempt 1/3)'
    )
    assert bodies[1] == (
        'fixing draft validation errors for candidate 3/7 "Edge Case" (attempt 2/3)'
    )
    assert bodies[2] == (
        'fixing draft validation errors for candidate 3/7 "Edge Case" (attempt 3/3)'
    )

    # All other comparable fields are identical across invocations
    reqs = runner.calls
    assert reqs[0].name == reqs[1].name == reqs[2].name
    assert reqs[0].prompt == reqs[1].prompt == reqs[2].prompt
    assert reqs[0].model == reqs[1].model == reqs[2].model
    assert reqs[0].role == reqs[1].role == reqs[2].role
    assert reqs[0].stage == reqs[1].stage == reqs[2].stage
