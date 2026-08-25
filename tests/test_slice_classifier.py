from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from pycastle.agents.slice_classifier import (
    ConcreteSliceVerdict,
    SliceClassifierVerdict,
    UncertainSliceVerdict,
    classify_slice,
    parse_classifier_output,
)
from pycastle.config.types import StageOverride
from pycastle.display.rows import StatusRowConfig, status_row
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.execution_contracts import (
    RuntimeInvocationDependencies,
    RuntimeStatusDisplay,
    RuntimeStatusRowConfig,
    WorkSessionState,
    WorktreeMount,
)
from pycastle.issue_readiness import SliceMode
from pycastle.runtime_session import RunKind
from pycastle.services.runtime_services import ToolPolicy
from pycastle.services.service_registry import ServiceRegistry

if TYPE_CHECKING:
    from pathlib import Path


def _status_row_factory(status_display, caller, *, kind, must_close, config=None):
    _cfg = config or RuntimeStatusRowConfig()
    return status_row(
        status_display,
        caller,
        kind=kind,
        must_close=must_close,
        config=StatusRowConfig(
            color_key=_cfg.color_key,
            work_body=_cfg.work_body,
            initial_phase=_cfg.initial_phase,
            startup_message=_cfg.startup_message,
            model_display=None,
        ),
    )


# ── Shared fake infrastructure for classify_slice tests ──────────────────────


class _CapturingRunner:
    """Records work_text invocations including the prompt and tool_policy."""

    def __init__(self, return_value: str = "{}") -> None:
        self.calls: list[dict] = []
        self._return_value = return_value

    async def setup(self, git_name: str, git_email: str) -> None:
        pass

    async def work(self, role, prompt, *, run_kind=RunKind.FRESH, **kwargs):
        raise AssertionError("classify_slice must route through work_text")

    async def work_text(
        self,
        prompt: str,
        *,
        role,
        tool_policy=ToolPolicy.FULL,
        session: WorkSessionState | None = None,
    ) -> str:
        self.calls.append({"prompt": prompt, "tool_policy": tool_policy})
        if session is not None and session.on_provider_session_id is not None:
            session.on_provider_session_id("test-session-id")
        return self._return_value


class _SimpleService:
    def __init__(self, name: str) -> None:
        self.name = name

    def build_env(self, state_dir_container_path=None, token=None) -> dict[str, str]:
        return {}

    def is_available(self, now=None, *, model=None) -> bool:
        return True

    def next_wake_time(self) -> datetime:
        return datetime(2030, 1, 1, tzinfo=UTC)

    def mark_exhausted(self, reset_time, *, _now=None) -> None:
        pass

    def mark_model_restricted(self, model: str) -> None:
        pass

    def state_dir_relpath(self, role, namespace: str = "") -> str | None:
        return None

    def is_resumable(self, state_dir) -> bool:
        return False

    def valid_models(self) -> set:
        return set()


def _make_session_mock() -> MagicMock:
    prepared_session = MagicMock()
    prepared_session.provider_state_dir_container_path = None
    provider_run_session = MagicMock()
    provider_run_session.run_kind = RunKind.FRESH
    provider_run_session.provider_session_id = None
    prepared_session.initial_provider_run_session.return_value = provider_run_session
    prepared_session.resumable_provider_run_session.return_value = provider_run_session
    prepared_session.protocol_reprompt_provider_run_session.return_value = None
    return prepared_session


def _deps_for_runner(runner: _CapturingRunner) -> RuntimeInvocationDependencies:
    return RuntimeInvocationDependencies(
        container_workspace="/workspace",
        timeout_retries=0,
        stage_key_for_role=lambda _: None,
        prepare_session=lambda _: _make_session_mock(),
        build_session=lambda *_: MagicMock(),
        build_runner=lambda *_: runner,  # type: ignore[arg-type]
        get_git_identity=lambda: ("Test User", "test@example.com"),
        status_display_factory=lambda: cast(
            "RuntimeStatusDisplay", PlainStatusDisplay()
        ),
        status_row_factory=_status_row_factory,
        handle_provider_account_exhaustion=lambda svc, err: svc.mark_exhausted(
            err.reset_time
        ),
    )


class _FakeAdapter:
    def __init__(self, service: _SimpleService, runner: _CapturingRunner) -> None:
        self._service = service
        self._runner = runner

    def resolve_service(self, service_name: str = "") -> _SimpleService:
        return self._service

    def build_work_dependencies(
        self, *, name, model, effort, service
    ) -> RuntimeInvocationDependencies:
        return _deps_for_runner(self._runner)


def _make_worktree(tmp_path: Path) -> WorktreeMount:
    wt = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    wt.mkdir(parents=True)
    return WorktreeMount(host_path=wt)


# ── Behavior 1: Concrete slice-mode verdicts ──────────────────────────────────


@pytest.mark.parametrize(
    ("mode_key", "expected_mode"),
    [
        ("behavior", SliceMode.BEHAVIOR),
        ("refactor", SliceMode.REFACTOR),
        ("docs", SliceMode.DOCS),
    ],
)
def test_concrete_mode_output_yields_concrete_verdict(mode_key, expected_mode):
    raw = json.dumps({"mode": mode_key})
    verdict = parse_classifier_output(raw)
    assert isinstance(verdict, ConcreteSliceVerdict)
    assert verdict.mode is expected_mode


# ── Behavior 2: Uncertainty verdict carries the model's reason ────────────────


def test_uncertain_output_yields_uncertain_verdict_with_reason():
    reason = "Cannot determine if changes are behavioral or docs-only."
    raw = json.dumps({"uncertain": True, "reason": reason})
    verdict = parse_classifier_output(raw)
    assert isinstance(verdict, UncertainSliceVerdict)
    assert verdict.reason == reason


# ── Behavior 3: Malformed/empty/unexpected output falls back safely ───────────


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json at all",
        "{}",
        '{"mode": "unknown_mode"}',
        '{"something": "else"}',
        "null",
        "42",
        '["behavior"]',
        '{"mode": "BEHAVIOR"}',
        '{"mode": "Refactor"}',
        '{"mode": 1}',
        '{"reason": "   "}',
        '{"reason": null}',
    ],
)
def test_malformed_or_unexpected_output_yields_uncertain_verdict(raw):
    verdict = parse_classifier_output(raw)
    assert isinstance(verdict, UncertainSliceVerdict)


def test_whitespace_only_reason_uses_fallback_not_whitespace():
    verdict = parse_classifier_output('{"reason": "   "}')
    assert isinstance(verdict, UncertainSliceVerdict)
    assert verdict.reason.strip() != ""


def test_uncertain_reason_is_stripped():
    verdict = parse_classifier_output('{"reason": "  leading and trailing  "}')
    assert isinstance(verdict, UncertainSliceVerdict)
    assert verdict.reason == "leading and trailing"


# ── Behavior 4: Verdict type is shared ───────────────────────────────────────


def test_both_verdict_variants_are_importable_from_slice_classifier():
    # Both concrete and uncertain verdicts come from the same module, so
    # downstream label logic and classifier invocation share the same type.
    concrete: SliceClassifierVerdict = ConcreteSliceVerdict(mode=SliceMode.BEHAVIOR)
    uncertain: SliceClassifierVerdict = UncertainSliceVerdict(reason="unclear")
    assert isinstance(concrete, ConcreteSliceVerdict)
    assert isinstance(uncertain, UncertainSliceVerdict)


def test_parse_classifier_output_returns_slice_classifier_verdict_for_any_input():
    # parse_classifier_output always returns a SliceClassifierVerdict regardless
    # of whether the input is concrete or uncertain.
    concrete_verdict = parse_classifier_output(json.dumps({"mode": "refactor"}))
    uncertain_verdict = parse_classifier_output(
        json.dumps({"uncertain": True, "reason": "not sure"})
    )
    assert isinstance(concrete_verdict, ConcreteSliceVerdict | UncertainSliceVerdict)
    assert isinstance(uncertain_verdict, ConcreteSliceVerdict | UncertainSliceVerdict)


# ── Behavior 5: classify_slice — read-only one-shot call returns parsed verdict ─


def test_classify_slice_uses_restricted_tool_policy_and_returns_parsed_verdict(
    tmp_path,
):
    service = _SimpleService("claude")
    runner = _CapturingRunner(return_value='{"mode": "behavior"}')
    adapter = _FakeAdapter(service, runner)
    registry = ServiceRegistry({"claude": service})
    override = StageOverride(service="claude", model="haiku", effort="low")

    verdict = asyncio.run(
        classify_slice(
            issue_title="Add retry logic for API calls",
            issue_body="When an API call fails with a transient error, retry up to 3 times.",
            worktree=_make_worktree(tmp_path),
            plan_override=override,
            runner=adapter,
            service_registry=registry,
        )
    )

    assert len(runner.calls) == 1
    assert runner.calls[0]["tool_policy"] is ToolPolicy.RESTRICTED
    assert isinstance(verdict, ConcreteSliceVerdict)
    assert verdict.mode is SliceMode.BEHAVIOR


def test_classify_slice_prompt_contains_issue_title_and_body(tmp_path):
    service = _SimpleService("claude")
    runner = _CapturingRunner(return_value='{"mode": "refactor"}')
    adapter = _FakeAdapter(service, runner)
    registry = ServiceRegistry({"claude": service})
    override = StageOverride(service="claude", model="haiku", effort="low")
    title = "Rename FooService to BarService across codebase"
    body = "All references to FooService should become BarService."

    asyncio.run(
        classify_slice(
            issue_title=title,
            issue_body=body,
            worktree=_make_worktree(tmp_path),
            plan_override=override,
            runner=adapter,
            service_registry=registry,
        )
    )

    prompt = runner.calls[0]["prompt"]
    assert title in prompt
    assert body in prompt


def test_classify_slice_prompt_contains_slice_mode_definitions(tmp_path):
    service = _SimpleService("claude")
    runner = _CapturingRunner(return_value='{"mode": "docs"}')
    adapter = _FakeAdapter(service, runner)
    registry = ServiceRegistry({"claude": service})
    override = StageOverride(service="claude", model="haiku", effort="low")

    asyncio.run(
        classify_slice(
            issue_title="Update CONTEXT.md with new glossary terms",
            issue_body="Add definitions for the new agent roles introduced in ADR-0060.",
            worktree=_make_worktree(tmp_path),
            plan_override=override,
            runner=adapter,
            service_registry=registry,
        )
    )

    prompt = runner.calls[0]["prompt"]
    assert "behavior-slice" in prompt
    assert "refactor-slice" in prompt
    assert "docs-slice" in prompt


def test_classify_slice_prompt_excludes_other_issue_metadata(tmp_path):
    service = _SimpleService("claude")
    runner = _CapturingRunner(return_value='{"mode": "refactor"}')
    adapter = _FakeAdapter(service, runner)
    registry = ServiceRegistry({"claude": service})
    override = StageOverride(service="claude", model="haiku", effort="low")

    asyncio.run(
        classify_slice(
            issue_title="Rename method",
            issue_body="Rename foo to bar.",
            worktree=_make_worktree(tmp_path),
            plan_override=override,
            runner=adapter,
            service_registry=registry,
        )
    )

    prompt = runner.calls[0]["prompt"]
    # The function signature only accepts title and body — no labels, comments,
    # or parent body can reach the prompt.
    assert "ready-for-agent" not in prompt
    assert "needs-triage" not in prompt
