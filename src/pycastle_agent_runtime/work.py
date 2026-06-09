from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from .contracts import AgentService, ToolPolicy
from .roles import AgentRole
from .session import RunKind

if TYPE_CHECKING:
    from pycastle.errors import (
        AgentTimeoutError,
        TransientAgentError,
    )

WorkResultT = TypeVar("WorkResultT")


def _default_prepare_session(**kwargs: Any) -> Any:
    from pycastle.session.run_dispatch import RunSessionRequest, prepare_run_session

    return prepare_run_session(
        RunSessionRequest(
            worktree=kwargs["mount_path"],
            role=kwargs["role"],
            session_namespace=kwargs["session_namespace"],
            service=kwargs["service"],
            container_workspace=kwargs["container_workspace"],
            run_session_plan=kwargs["run_session_plan"],
        )
    )


def _default_status_row_factory(*args: Any, **kwargs: Any) -> Any:
    from pycastle.iteration._rows import status_row

    return status_row(*args, **kwargs)


def _invoke_prepare_session(
    prepare_session: Callable[..., Any],
    *,
    mount_path: Path,
    role: AgentRole,
    session_namespace: str,
    service: AgentService,
    container_workspace: str,
    run_session_plan: Any,
) -> Any:
    kwargs = {
        "mount_path": mount_path,
        "role": role,
        "session_namespace": session_namespace,
        "service": service,
        "container_workspace": container_workspace,
        "run_session_plan": run_session_plan,
    }
    parameters = tuple(inspect.signature(prepare_session).parameters.values())
    if (
        not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
        and len(parameters) == 1
    ):
        from pycastle.session.run_dispatch import RunSessionRequest

        return prepare_session(
            RunSessionRequest(
                worktree=kwargs["mount_path"],
                role=kwargs["role"],
                session_namespace=kwargs["session_namespace"],
                service=kwargs["service"],
                container_workspace=kwargs["container_workspace"],
                run_session_plan=kwargs["run_session_plan"],
            )
        )
    return prepare_session(**kwargs)


@dataclasses.dataclass
class CancellationToken:
    _event: asyncio.Event = dataclasses.field(
        default_factory=asyncio.Event,
        init=False,
        repr=False,
    )

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()


class WorkExecutionAdapter(Protocol):
    async def setup(
        self, git_name: str, git_email: str, work_body: str = ""
    ) -> None: ...

    async def work(
        self,
        role: AgentRole,
        prompt: str,
        *,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Any: ...

    async def work_text(
        self,
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy: Any = ToolPolicy.FULL,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> str: ...


class WorkOutputAdapter(Protocol[WorkResultT]):
    async def build_prompt(
        self,
        *,
        run_kind: RunKind,
        container_exec: Callable[[str], Awaitable[str]],
    ) -> str: ...

    async def invoke(
        self,
        *,
        runner: WorkExecutionAdapter,
        role: AgentRole,
        prompt: str,
        run_kind: RunKind,
        session_uuid: str | None,
        on_provider_session_id: Callable[[str], None],
    ) -> WorkResultT: ...

    def is_successful_result(self, result: WorkResultT) -> bool: ...

    def protocol_reprompt_message(self) -> str | None: ...

    def protocol_error_result(self) -> WorkResultT | None: ...

    def protocol_error_types(self) -> tuple[type[BaseException], ...]: ...

    def non_typed_failure_result(self) -> WorkResultT | None: ...

    def finalize_result(
        self,
        result: WorkResultT,
        *,
        role: AgentRole,
        mount_path: Path,
        session_namespace: str,
        service_name: str,
    ) -> WorkResultT: ...


@dataclasses.dataclass(frozen=True)
class WorkInvocationDependencies:
    container_workspace: str
    timeout_retries: int
    stage_key_for_role: Callable[[AgentRole], str | None]
    build_session: Callable[[Path, AgentService, str | None], Any]
    build_runner: Callable[[Any, Any], WorkExecutionAdapter]
    get_git_identity: Callable[[], tuple[str, str]]
    prepare_session: Callable[..., Any] = _default_prepare_session
    status_row_factory: Callable[..., AbstractAsyncContextManager[Any]] = (
        _default_status_row_factory
    )
    setup_error_types: tuple[type[BaseException], ...] = ()
    build_setup_phase_error: (
        Callable[[AgentRole, BaseException], BaseException] | None
    ) = None
    transient_status_message: Callable[[TransientAgentError], str] | None = None


@dataclasses.dataclass(frozen=True)
class WorkInvocationRequest(Generic[WorkResultT]):
    name: str
    mount_path: Path
    role: AgentRole
    service: AgentService
    model: str
    effort: str
    output_adapter: WorkOutputAdapter[WorkResultT] = dataclasses.field(repr=False)
    dependencies: WorkInvocationDependencies = dataclasses.field(repr=False)
    status_display: Any = None
    token: CancellationToken | None = None
    work_body: str = ""
    session_namespace: str = ""
    run_session_plan: Any = None
    color_key: int | None = None
    allow_non_typed_resume_retry: bool = False


@dataclasses.dataclass(frozen=True)
class TextOutputAdapter:
    prompt: str
    tool_policy: Any = ToolPolicy.FULL

    async def build_prompt(
        self,
        *,
        run_kind: RunKind,
        container_exec: Callable[[str], Awaitable[str]],
    ) -> str:
        del run_kind, container_exec
        return self.prompt

    async def invoke(
        self,
        *,
        runner: WorkExecutionAdapter,
        role: AgentRole,
        prompt: str,
        run_kind: RunKind,
        session_uuid: str | None,
        on_provider_session_id: Callable[[str], None],
    ) -> str:
        return await runner.work_text(
            prompt,
            role=role,
            tool_policy=self.tool_policy,
            run_kind=run_kind,
            session_uuid=session_uuid,
            on_provider_session_id=on_provider_session_id,
        )

    def is_successful_result(self, result: str) -> bool:
        return True

    def protocol_reprompt_message(self) -> str | None:
        return None

    def protocol_error_result(self) -> str | None:
        return None

    def non_typed_failure_result(self) -> str | None:
        return None

    def protocol_error_types(self) -> tuple[type[BaseException], ...]:
        return ()

    def finalize_result(
        self,
        result: str,
        *,
        role: AgentRole,
        mount_path: Path,
        session_namespace: str,
        service_name: str,
    ) -> str:
        del role, mount_path, session_namespace, service_name
        return result


def _ensure_timeout_context(
    error: "AgentTimeoutError",
    *,
    role: AgentRole,
    mount_path: Path,
) -> "AgentTimeoutError":
    if not error.role_value:
        error.role_value = role.value
        error.worktree_path = mount_path
    return error


async def invoke_work(request: WorkInvocationRequest[WorkResultT]) -> WorkResultT:
    status_display = request.status_display
    if status_display is None:
        from pycastle.display.status_display import PlainStatusDisplay

        status_display = PlainStatusDisplay()

    token = request.token if request.token is not None else CancellationToken()
    if token.is_cancelled:
        from pycastle.errors import UsageLimitError

        raise UsageLimitError(
            reset_time=None,
            stage_key=request.dependencies.stage_key_for_role(request.role),
        )

    prepared_session = _invoke_prepare_session(
        request.dependencies.prepare_session,
        mount_path=request.mount_path,
        role=request.role,
        session_namespace=request.session_namespace,
        service=request.service,
        container_workspace=request.dependencies.container_workspace,
        run_session_plan=request.run_session_plan,
    )
    non_typed_retry_done = False
    initial_attempt = True

    async with request.dependencies.status_row_factory(
        status_display,
        request.name,
        kind="agent",
        must_close=False,
        work_body=request.work_body,
        color_key=request.color_key,
        model_display=_model_display_metadata(
            service=request.service.name,
            model=request.model,
            effort=request.effort,
        ),
    ) as row:
        session = request.dependencies.build_session(
            request.mount_path,
            request.service,
            prepared_session.provider_state_dir_container_path,
        )
        runner = request.dependencies.build_runner(session, status_display)
        try:
            from pycastle.errors import (
                AgentCredentialFailureError,
                AgentTimeoutError,
                HardAgentError,
                TransientAgentError,
                UsageLimitError,
            )
            from pycastle.services.claude_service import ClaudeService

            git_name, git_email = request.dependencies.get_git_identity()
            try:
                await runner.setup(git_name, git_email, request.work_body)
            except Exception as exc:
                if request.dependencies.setup_error_types and isinstance(
                    exc, request.dependencies.setup_error_types
                ):
                    if request.dependencies.build_setup_phase_error is not None:
                        raise request.dependencies.build_setup_phase_error(
                            request.role, exc
                        ) from exc
                if request.dependencies.build_setup_phase_error is None:
                    from pycastle.errors import DockerError, SetupPhaseError

                    if isinstance(exc, DockerError):
                        raise SetupPhaseError(request.role.value, str(exc)) from exc
                raise

            prepared_session.prepare_for_run()
            loop = asyncio.get_running_loop()

            async def container_exec(cmd: str) -> str:
                return await loop.run_in_executor(None, session.exec_simple, cmd)

            retries_left = request.dependencies.timeout_retries
            while True:
                provider_run_session = (
                    prepared_session.initial_provider_run_session()
                    if initial_attempt
                    else prepared_session.resumable_provider_run_session()
                )
                try:
                    prompt = await request.output_adapter.build_prompt(
                        run_kind=provider_run_session.run_kind,
                        container_exec=container_exec,
                    )
                    result, successful_run_session = await _invoke_work_attempt(
                        request=request,
                        row=row,
                        prepared_session=prepared_session,
                        runner=runner,
                        prompt=prompt,
                        provider_run_session=provider_run_session,
                    )
                    if request.output_adapter.is_successful_result(result):
                        successful_run_session.record_successful_run()
                    else:
                        row.close("failed", shutdown_style="error")
                    return request.output_adapter.finalize_result(
                        result,
                        role=request.role,
                        mount_path=request.mount_path,
                        session_namespace=request.session_namespace,
                        service_name=request.service.name,
                    )
                except AgentTimeoutError as err:
                    _ensure_timeout_context(
                        err,
                        role=request.role,
                        mount_path=request.mount_path,
                    )
                    if retries_left <= 0:
                        raise
                    restart_num = (
                        request.dependencies.timeout_retries - retries_left + 1
                    )
                    status_display.print(
                        request.name,
                        "Timeout — restarting"
                        f" (attempt {restart_num}/{request.dependencies.timeout_retries})",
                    )
                    retries_left -= 1
                    initial_attempt = False
                except UsageLimitError as err:
                    if err.stage_key is None:
                        err.stage_key = request.dependencies.stage_key_for_role(
                            request.role
                        )
                    if err.is_permanent and isinstance(request.service, ClaudeService):
                        err.account_label = request.service.mark_permanently_exhausted()
                    else:
                        request.service.mark_exhausted(err.reset_time)
                    token.cancel()
                    raise
                except TransientAgentError as err:
                    token.cancel()
                    if request.dependencies.transient_status_message is not None:
                        status_display.print(
                            request.name,
                            request.dependencies.transient_status_message(err),
                        )
                    raise
                except AgentCredentialFailureError as err:
                    token.cancel()
                    err.caller = request.name
                    if (not err.service_name) or (
                        err.service_name == "claude"
                        and request.service.name != "claude"
                    ):
                        err.service_name = request.service.name
                    raise
                except HardAgentError as err:
                    token.cancel()
                    err.caller = request.name
                    err.service_name = request.service.name
                    raise
                except Exception:
                    if (
                        not request.allow_non_typed_resume_retry
                        or provider_run_session.run_kind != RunKind.RESUME
                    ):
                        raise
                    failure_result = request.output_adapter.non_typed_failure_result()
                    if failure_result is None:
                        raise
                    if non_typed_retry_done:
                        row.close("failed", shutdown_style="error")
                        return request.output_adapter.finalize_result(
                            failure_result,
                            role=request.role,
                            mount_path=request.mount_path,
                            session_namespace=request.session_namespace,
                            service_name=request.service.name,
                        )
                    non_typed_retry_done = True
        finally:
            try:
                session.__exit__(None, None, None)
            except Exception:
                pass


async def _invoke_work_attempt(
    *,
    request: WorkInvocationRequest[WorkResultT],
    row: Any,
    prepared_session: Any,
    runner: WorkExecutionAdapter,
    prompt: str,
    provider_run_session: Any,
) -> tuple[WorkResultT, Any]:
    reprompt_message = request.output_adapter.protocol_reprompt_message()
    protocol_error_result = request.output_adapter.protocol_error_result()
    protocol_error_types = request.output_adapter.protocol_error_types()
    max_attempts = (
        3 if reprompt_message is not None and protocol_error_result is not None else 1
    )
    work_prompt = prompt
    work_run_session = provider_run_session
    for _ in range(max_attempts):
        try:
            result = await request.output_adapter.invoke(
                runner=runner,
                role=request.role,
                prompt=work_prompt,
                run_kind=work_run_session.run_kind,
                session_uuid=work_run_session.provider_session_id,
                on_provider_session_id=work_run_session.record_provider_session_id,
            )
            return result, work_run_session
        except Exception as exc:
            if not protocol_error_types or not isinstance(exc, protocol_error_types):
                raise
            if reprompt_message is None or protocol_error_result is None:
                raise
            next_run_session = prepared_session.protocol_reprompt_provider_run_session()
            if next_run_session is None:
                row.close("failed", shutdown_style="error")
                return protocol_error_result, work_run_session
            work_prompt = reprompt_message
            work_run_session = next_run_session
    row.close("failed", shutdown_style="error")
    assert protocol_error_result is not None
    return protocol_error_result, work_run_session


def _model_display_metadata(*, service: str, model: str, effort: str) -> Any:
    from pycastle.display.status_display import ModelDisplayMetadata

    return ModelDisplayMetadata(service=service, model=model, effort=effort)


__all__ = [
    "CancellationToken",
    "TextOutputAdapter",
    "WorkExecutionAdapter",
    "WorkInvocationDependencies",
    "WorkInvocationRequest",
    "WorkOutputAdapter",
    "invoke_work",
]
