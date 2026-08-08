"""Tests for one-shot invocation tool policy enforcement (issue #2067)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    from pathlib import Path

from pycastle.config.types import StageOverride
from pycastle.errors import UsageLimitError
from pycastle.execution_contracts import (
    RuntimeInvocationDependencies,
    WorkSessionState,
    WorktreeMount,
)
from pycastle.runtime import OneShotRunRequest, run_one_shot
from pycastle.runtime_session import RunKind
from pycastle.services.runtime_services import ToolPolicy
from pycastle.services.service_registry import ServiceRegistry

# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Minimal RuntimeExecutionAdapter that records work_text invocations."""

    def __init__(self, return_value: str = "output") -> None:
        self.work_text_calls: list[dict] = []
        self._return_value = return_value

    async def setup(self, git_name: str, git_email: str) -> None:
        pass

    async def work(
        self,
        role,
        prompt,
        *,
        run_kind=RunKind.FRESH,
        session_uuid=None,
        on_provider_session_id=None,
    ):
        raise AssertionError(
            "work() was called; one-shot must route through work_text() so "
            "tool_policy is honoured"
        )

    async def work_text(
        self,
        prompt: str,
        *,
        role,
        tool_policy=ToolPolicy.FULL,
        session: WorkSessionState | None = None,
    ) -> str:
        self.work_text_calls.append({"tool_policy": tool_policy})
        if session is not None and session.on_provider_session_id is not None:
            session.on_provider_session_id("test-session-id")
        return self._return_value


class _ExhaustingService:
    """Service that becomes unavailable after mark_exhausted() is called."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._exhausted = False

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        return {}

    def is_available(self, now=None, *, model=None) -> bool:
        return not self._exhausted

    def next_wake_time(self) -> datetime:
        return datetime(2030, 1, 1, tzinfo=UTC)

    def mark_exhausted(self, reset_time, *, _now=None) -> None:
        self._exhausted = True

    def mark_model_restricted(self, model: str) -> None:
        pass

    def state_dir_relpath(self, role, namespace: str = "") -> str | None:
        return None

    def is_resumable(self, state_dir: Path) -> bool:
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


def _deps_for_runner(runner: _RecordingRunner) -> RuntimeInvocationDependencies:
    return RuntimeInvocationDependencies(
        container_workspace="/workspace",
        timeout_retries=0,
        stage_key_for_role=lambda _: None,
        prepare_session=lambda _: _make_session_mock(),
        build_session=lambda *_: MagicMock(),
        build_runner=lambda *_: runner,  # type: ignore[arg-type]
        get_git_identity=lambda: ("Test User", "test@example.com"),
        handle_provider_account_exhaustion=lambda svc, err: svc.mark_exhausted(
            err.reset_time
        ),
    )


class _FakeSingleServiceAdapter:
    """Execution adapter backed by one service and one runner."""

    def __init__(self, service: _ExhaustingService, runner: _RecordingRunner) -> None:
        self._service = service
        self._runner = runner

    def resolve_service(self, service_name: str = "") -> _ExhaustingService:
        return self._service

    def build_work_dependencies(
        self, *, name, model, effort, service
    ) -> RuntimeInvocationDependencies:
        return _deps_for_runner(self._runner)


class _FakeTwoServiceAdapter:
    """Execution adapter backed by a primary and a fallback service/runner pair."""

    def __init__(
        self,
        primary: _ExhaustingService,
        primary_runner: _RecordingRunner,
        fallback: _ExhaustingService,
        fallback_runner: _RecordingRunner,
    ) -> None:
        self._primary = primary
        self._primary_runner = primary_runner
        self._fallback = fallback
        self._fallback_runner = fallback_runner

    def resolve_service(self, service_name: str = "") -> _ExhaustingService:
        if service_name == self._primary.name:
            return self._primary
        return self._fallback

    def build_work_dependencies(
        self, *, name, model, effort, service
    ) -> RuntimeInvocationDependencies:
        runner = (
            self._primary_runner
            if service.name == self._primary.name
            else self._fallback_runner
        )
        return _deps_for_runner(runner)


def _make_worktree(tmp_path: Path) -> WorktreeMount:
    wt = tmp_path / "pycastle" / ".worktrees" / "one-shot-sandbox"
    wt.mkdir(parents=True)
    return WorktreeMount(host_path=wt)


# ---------------------------------------------------------------------------
# Behavior 1 — RESTRICTED policy invokes provider with read-and-search access
# ---------------------------------------------------------------------------


def test_one_shot_restricted_policy_invokes_provider_with_restricted_tool_access(
    tmp_path,
):
    service = _ExhaustingService("claude")
    runner = _RecordingRunner()
    adapter = _FakeSingleServiceAdapter(service, runner)
    registry = ServiceRegistry({"claude": service})
    override = StageOverride(service="claude", model="sonnet", effort="medium")

    request = OneShotRunRequest(
        prompt="classify this issue",
        worktree=_make_worktree(tmp_path),
        override=override,
        tool_policy=ToolPolicy.RESTRICTED,
        name="Slice Classifier",
    )

    asyncio.run(
        run_one_shot(runner=adapter, service_registry=registry, request=request)
    )

    assert len(runner.work_text_calls) == 1
    assert runner.work_text_calls[0]["tool_policy"] is ToolPolicy.RESTRICTED


# ---------------------------------------------------------------------------
# Behavior 2 — FULL policy invokes provider with full (unrestricted) access
# ---------------------------------------------------------------------------


def test_one_shot_full_policy_invokes_provider_with_full_tool_access(tmp_path):
    service = _ExhaustingService("claude")
    runner = _RecordingRunner()
    adapter = _FakeSingleServiceAdapter(service, runner)
    registry = ServiceRegistry({"claude": service})
    override = StageOverride(service="claude", model="sonnet", effort="medium")

    request = OneShotRunRequest(
        prompt="do some work",
        worktree=_make_worktree(tmp_path),
        override=override,
        tool_policy=ToolPolicy.FULL,
        name="One Shot Agent",
    )

    asyncio.run(
        run_one_shot(runner=adapter, service_registry=registry, request=request)
    )

    assert len(runner.work_text_calls) == 1
    assert runner.work_text_calls[0]["tool_policy"] is ToolPolicy.FULL


# ---------------------------------------------------------------------------
# Behavior 3 — Service rotation on usage limit works regardless of tool policy
# ---------------------------------------------------------------------------


class _UsageLimitOnFirstCallRunner(_RecordingRunner):
    """Raises UsageLimitError on the first work_text call, then succeeds."""

    def __init__(self) -> None:
        super().__init__(return_value="fallback output")
        self._calls = 0

    async def work_text(
        self, prompt, *, role, tool_policy=ToolPolicy.FULL, session=None
    ):
        self._calls += 1
        if self._calls == 1:
            raise UsageLimitError(reset_time=None, provider="primary")
        return await super().work_text(
            prompt, role=role, tool_policy=tool_policy, session=session
        )


def test_one_shot_rotates_to_fallback_service_on_usage_limit_with_restricted_policy(
    tmp_path,
):
    primary = _ExhaustingService("primary")
    fallback = _ExhaustingService("fallback")
    primary_runner = _UsageLimitOnFirstCallRunner()
    fallback_runner = _RecordingRunner(return_value="fallback result")
    adapter = _FakeTwoServiceAdapter(primary, primary_runner, fallback, fallback_runner)
    registry = ServiceRegistry({"primary": primary, "fallback": fallback})
    fallback_override = StageOverride(
        service="fallback", model="sonnet", effort="medium"
    )
    override = StageOverride(
        service="primary",
        model="sonnet",
        effort="medium",
        fallback=fallback_override,
    )

    request = OneShotRunRequest(
        prompt="classify this",
        worktree=_make_worktree(tmp_path),
        override=override,
        tool_policy=ToolPolicy.RESTRICTED,
        name="Slice Classifier",
    )

    result = asyncio.run(
        run_one_shot(runner=adapter, service_registry=registry, request=request)
    )

    assert result.used_fallback is True
    assert result.selected_service == "fallback"
    assert len(fallback_runner.work_text_calls) == 1
    assert fallback_runner.work_text_calls[0]["tool_policy"] is ToolPolicy.RESTRICTED
