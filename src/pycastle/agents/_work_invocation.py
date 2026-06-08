from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from ..display.status_display import ModelDisplayMetadata, PlainStatusDisplay
from ..errors import (
    AgentFailedError,
    AgentCredentialFailureError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from ..infrastructure.container_runner import ContainerRunner
from ..services.agent_service import AgentService
from ..services.claude_service import ClaudeService
from ..services.flag_profiles import AgentToolPolicyGroup
from ..session.agent import RunSessionPlan
from ..session.resume import RunKind
from .output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    FailedOutput,
)
from .result import CancellationToken
from .session_dispatch import SessionDispatchRequest, prepare_agent_session

WorkResultT = TypeVar("WorkResultT")


class WorkPromptFactory(Protocol):
    async def __call__(
        self,
        *,
        run_kind: RunKind,
        container_exec: Callable[[str], Awaitable[str]],
    ) -> str: ...


class WorkOutputAdapter(Protocol[WorkResultT]):
    async def invoke(
        self,
        *,
        runner: ContainerRunner,
        role: AgentRole,
        prompt: str,
        run_kind: RunKind,
        session_uuid: str | None,
        on_provider_session_id: Callable[[str], None],
    ) -> WorkResultT: ...

    def is_successful_result(self, result: WorkResultT) -> bool: ...

    def protocol_reprompt_message(self) -> str | None: ...

    def protocol_error_result(self) -> WorkResultT | None: ...

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
    build_runner: Callable[[Any, Any], ContainerRunner]
    get_git_identity: Callable[[], tuple[str, str]]
    transient_status_message: Callable[[TransientAgentError], str] | None = None


@dataclasses.dataclass(frozen=True)
class WorkInvocationRequest(Generic[WorkResultT]):
    name: str
    mount_path: Path
    role: AgentRole
    service: AgentService
    model: str
    effort: str
    prompt_factory: WorkPromptFactory = dataclasses.field(repr=False)
    output_adapter: WorkOutputAdapter[WorkResultT] = dataclasses.field(repr=False)
    dependencies: WorkInvocationDependencies = dataclasses.field(repr=False)
    status_display: Any = None
    token: CancellationToken | None = None
    work_body: str = ""
    session_namespace: str = ""
    run_session_plan: RunSessionPlan | None = None
    color_key: int | None = None
    allow_non_typed_resume_retry: bool = False


@dataclasses.dataclass(frozen=True)
class ProtocolOutputAdapter:
    reprompt_message: str

    async def invoke(
        self,
        *,
        runner: ContainerRunner,
        role: AgentRole,
        prompt: str,
        run_kind: RunKind,
        session_uuid: str | None,
        on_provider_session_id: Callable[[str], None],
    ) -> AgentOutput:
        return await runner.work(
            role,
            prompt,
            run_kind=run_kind,
            session_uuid=session_uuid,
            on_provider_session_id=on_provider_session_id,
        )

    def is_successful_result(self, result: AgentOutput) -> bool:
        return not isinstance(result, FailedOutput)

    def protocol_reprompt_message(self) -> str | None:
        return self.reprompt_message

    def protocol_error_result(self) -> AgentOutput | None:
        return FailedOutput(failure_class="protocol_error")

    def non_typed_failure_result(self) -> AgentOutput | None:
        return FailedOutput(failure_class="non_typed_crash")

    def finalize_result(
        self,
        result: AgentOutput,
        *,
        role: AgentRole,
        mount_path: Path,
        session_namespace: str,
        service_name: str,
    ) -> AgentOutput:
        if isinstance(result, FailedOutput):
            raise AgentFailedError(
                role_value=role.value,
                worktree_path=mount_path,
                namespace=session_namespace,
                failure_class=result.failure_class,
                service_name=service_name,
            )
        return result


@dataclasses.dataclass(frozen=True)
class TextOutputAdapter:
    tool_policy: AgentToolPolicyGroup = AgentToolPolicyGroup.FULL

    async def invoke(
        self,
        *,
        runner: ContainerRunner,
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


async def invoke_work(request: WorkInvocationRequest[WorkResultT]) -> WorkResultT:
    from ..iteration._rows import status_row

    status_display = request.status_display
    if status_display is None:
        status_display = PlainStatusDisplay()

    token = request.token if request.token is not None else CancellationToken()
    if token.is_cancelled:
        raise UsageLimitError(
            reset_time=None,
            stage_key=request.dependencies.stage_key_for_role(request.role),
        )

    prepared_session = prepare_agent_session(
        SessionDispatchRequest(
            mount_path=request.mount_path,
            role=request.role,
            session_namespace=request.session_namespace,
            service=request.service,
            container_workspace=request.dependencies.container_workspace,
            run_session_plan=request.run_session_plan,
        )
    )
    non_typed_retry_done = False
    initial_attempt = True

    async with status_row(
        status_display,
        request.name,
        kind="agent",
        must_close=False,
        work_body=request.work_body,
        color_key=request.color_key,
        model_display=ModelDisplayMetadata(
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
            git_name, git_email = request.dependencies.get_git_identity()
            try:
                await runner.setup(git_name, git_email, request.work_body)
            except DockerError as exc:
                raise SetupPhaseError(request.role.value, str(exc)) from exc

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
                    prompt = await request.prompt_factory(
                        run_kind=provider_run_session.run_kind,
                        container_exec=container_exec,
                    )
                    result = await _invoke_work_attempt(
                        request=request,
                        row=row,
                        prepared_session=prepared_session,
                        runner=runner,
                        prompt=prompt,
                        provider_run_session=provider_run_session,
                    )
                    if request.output_adapter.is_successful_result(result):
                        provider_run_session.record_successful_run()
                    else:
                        row.close("failed", shutdown_style="error")
                    return request.output_adapter.finalize_result(
                        result,
                        role=request.role,
                        mount_path=request.mount_path,
                        session_namespace=request.session_namespace,
                        service_name=request.service.name,
                    )
                except AgentTimeoutError:
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
    runner: ContainerRunner,
    prompt: str,
    provider_run_session: Any,
) -> WorkResultT:
    reprompt_message = request.output_adapter.protocol_reprompt_message()
    protocol_error_result = request.output_adapter.protocol_error_result()
    max_attempts = (
        3 if reprompt_message is not None and protocol_error_result is not None else 1
    )
    work_prompt = prompt
    work_run_session = provider_run_session
    for _ in range(max_attempts):
        try:
            return await request.output_adapter.invoke(
                runner=runner,
                role=request.role,
                prompt=work_prompt,
                run_kind=work_run_session.run_kind,
                session_uuid=work_run_session.provider_session_id,
                on_provider_session_id=work_run_session.record_provider_session_id,
            )
        except AgentOutputProtocolError:
            if reprompt_message is None or protocol_error_result is None:
                raise
            next_run_session = prepared_session.protocol_reprompt_provider_run_session()
            if next_run_session is None:
                row.close("failed", shutdown_style="error")
                return protocol_error_result
            work_prompt = reprompt_message
            work_run_session = next_run_session
    row.close("failed", shutdown_style="error")
    assert protocol_error_result is not None
    return protocol_error_result
