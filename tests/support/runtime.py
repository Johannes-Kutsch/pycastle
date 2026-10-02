"""Shared test helpers for RuntimeInvocationDependencies construction.

Exports:
- ``plain_status_display_factory`` / ``plain_runtime_status_row_factory`` — lightweight
  display stubs used across the test suite.
- ``_make_session_mock`` — shared factory for a ``PreparedRunSessionState`` MagicMock
  with the minimal attributes the runtime needs.
- ``make_runtime_invocation_dependencies`` — keyword-only builder that returns a
  fully-constructed ``RuntimeInvocationDependencies`` with sensible test defaults.

The builder is the single construction point intended to serve:
- ``tests/test_execute_runtime_request_teardown.py``
- ``tests/test_runtime_one_shot.py``
- ``tests/test_slice_classifier.py``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

from pycastle.display.rows import StatusRowConfig, status_row
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.execution_contracts import (
    RuntimeInvocationDependencies,
    RuntimeStatusDisplay,
    RuntimeStatusRowConfig,
)
from pycastle.runtime_session import RunKind

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from pycastle.agents.output_protocol import AgentRole
    from pycastle.execution_contracts import (
        MountPreconditionValidator,
        ProviderAccountExhaustionHandler,
        RuntimeExecutionAdapter,
        SessionStatePreparer,
        SetupFailureTranslator,
        StatusDisplayFactory,
        StatusRowFactory,
    )
    from pycastle.services.runtime_services import AgentService


def plain_status_display_factory() -> RuntimeStatusDisplay:
    return cast("RuntimeStatusDisplay", PlainStatusDisplay())


def plain_runtime_status_row_factory(
    status_display: Any,
    caller: str,
    *,
    kind: str,
    must_close: bool,
    config: RuntimeStatusRowConfig | None = None,
) -> AbstractAsyncContextManager[Any]:
    _cfg = config or RuntimeStatusRowConfig()
    return status_row(
        status_display,
        caller,
        kind=cast("Any", kind),
        must_close=must_close,
        config=StatusRowConfig(
            color_key=_cfg.color_key,
            work_body=_cfg.work_body,
            initial_phase=_cfg.initial_phase,
            startup_message=_cfg.startup_message,
        ),
    )


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


def make_runtime_invocation_dependencies(  # noqa: PLR0913
    *,
    container_workspace: str = "/workspace",
    timeout_retries: int = 0,
    stage_key_for_role: Callable[[AgentRole], str | None] = lambda _role: None,
    prepare_session: SessionStatePreparer | None = None,
    build_session: Callable[[Path, AgentService, str | None], Any] | None = None,
    build_runner: Callable[[Any, Any, Path | None], RuntimeExecutionAdapter]
    | None = None,
    get_git_identity: Callable[[], tuple[str, str]] = lambda: (
        "Test User",
        "test@example.com",
    ),
    status_display_factory: StatusDisplayFactory = plain_status_display_factory,
    status_row_factory: StatusRowFactory = plain_runtime_status_row_factory,
    handle_provider_account_exhaustion: ProviderAccountExhaustionHandler = (
        lambda svc, err: svc.mark_exhausted(err.reset_time)
    ),
    translate_setup_failure: SetupFailureTranslator | None = None,
    build_model_display_metadata: Callable[[str, str, str], Any | None] | None = None,
    validate_mount_preconditions: MountPreconditionValidator | None = None,
    transient_status_message: Callable[[Any], str] | None = None,
) -> RuntimeInvocationDependencies:
    if prepare_session is None:
        prepare_session = lambda _: _make_session_mock()  # noqa: E731
    if build_session is None:
        build_session = lambda *_: MagicMock()  # noqa: E731
    if build_runner is None:
        build_runner = lambda *_: MagicMock()  # noqa: E731

    return RuntimeInvocationDependencies(
        container_workspace=container_workspace,
        timeout_retries=timeout_retries,
        stage_key_for_role=stage_key_for_role,
        prepare_session=prepare_session,
        build_session=build_session,
        build_runner=build_runner,
        get_git_identity=get_git_identity,
        status_display_factory=status_display_factory,
        status_row_factory=status_row_factory,
        handle_provider_account_exhaustion=handle_provider_account_exhaustion,
        translate_setup_failure=translate_setup_failure,
        build_model_display_metadata=build_model_display_metadata,
        validate_mount_preconditions=validate_mount_preconditions,
        transient_status_message=transient_status_message,
    )
