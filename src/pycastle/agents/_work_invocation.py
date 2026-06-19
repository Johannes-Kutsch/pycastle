from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from pycastle_agent_runtime.errors import (
    AgentTimeoutError as RuntimeAgentTimeoutError,
    TransientAgentError as RuntimeTransientAgentError,
    UsageLimitError as RuntimeUsageLimitError,
)
from pycastle_agent_runtime.roles import AgentRole
from pycastle_agent_runtime.session import RunKind
from pycastle_agent_runtime.work import (
    CancellationToken,
    RunSessionPlan,
    TextOutputAdapter,
    WorkExecutionAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
    WorkOutputAdapter,
    WorkResultT,
    invoke_work as runtime_invoke_work,
)

from ..errors import (
    AgentFailedError,
    AgentTimeoutError,
    TransientAgentError,
    UsageLimitError,
)
from ..session.resume import provider_state_relpath
from .output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    FailedOutput,
)


class WorkPromptFactory(Protocol):
    async def __call__(
        self,
        *,
        run_kind: RunKind,
        container_exec: Callable[[str], Awaitable[str]],
    ) -> str: ...


@dataclasses.dataclass(frozen=True)
class ProtocolOutputAdapter:
    prompt_factory: WorkPromptFactory = dataclasses.field(repr=False)
    reprompt_message: str | Callable[[str | None], str]
    _last_protocol_error: str | None = dataclasses.field(init=False, default=None)

    async def build_prompt(
        self,
        *,
        run_kind: RunKind,
        container_exec: Callable[[str], Awaitable[str]],
    ) -> str:
        return await self.prompt_factory(
            run_kind=run_kind,
            container_exec=container_exec,
        )

    async def invoke(
        self,
        *,
        runner: WorkExecutionAdapter,
        role: AgentRole,
        prompt: str,
        run_kind: RunKind,
        session_uuid: str | None,
        on_provider_session_id: Callable[[str], None],
    ) -> AgentOutput:
        try:
            return await runner.work(
                role,
                prompt,
                run_kind=run_kind,
                session_uuid=session_uuid,
                on_provider_session_id=on_provider_session_id,
            )
        except AgentOutputProtocolError as exc:
            object.__setattr__(self, "_last_protocol_error", str(exc))
            raise

    def is_successful_result(self, result: AgentOutput) -> bool:
        return not isinstance(result, FailedOutput)

    def protocol_reprompt_message(self) -> str | None:
        if isinstance(self.reprompt_message, str):
            return self.reprompt_message
        return self.reprompt_message(self._last_protocol_error)

    def protocol_error_result(self) -> AgentOutput | None:
        return FailedOutput(failure_class="protocol_error")

    def protocol_error_types(self) -> tuple[type[BaseException], ...]:
        return (AgentOutputProtocolError,)

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
                provider_session_path=provider_state_relpath(
                    role, service_name, session_namespace
                ).rstrip("/"),
            )
        return result


def format_transient_status_message(err: RuntimeTransientAgentError) -> str:
    return (
        "transient API error: status "
        f"{err.status_code if err.status_code is not None else 'no status'}"
    )


async def invoke_work(request: WorkInvocationRequest[WorkResultT]) -> WorkResultT:
    try:
        return await runtime_invoke_work(request)
    except AgentTimeoutError:
        raise
    except UsageLimitError:
        raise
    except TransientAgentError:
        raise
    except RuntimeAgentTimeoutError as err:
        raise AgentTimeoutError(
            str(err),
            role_value=err.role_value,
            worktree_path=err.worktree_path,
        ) from err
    except RuntimeUsageLimitError as err:
        raise UsageLimitError(
            reset_time=err.reset_time,
            raw_message=err.raw_message,
            provider=err.provider,
            is_permanent=err.is_permanent,
            account_label=err.account_label,
            stage_key=err.stage_key,
        ) from err
    except RuntimeTransientAgentError as err:
        raise TransientAgentError(
            str(err),
            status_code=err.status_code,
        ) from err


__all__ = [
    "CancellationToken",
    "ProtocolOutputAdapter",
    "RunSessionPlan",
    "TextOutputAdapter",
    "WorkExecutionAdapter",
    "WorkInvocationDependencies",
    "WorkInvocationRequest",
    "WorkOutputAdapter",
    "format_transient_status_message",
    "invoke_work",
]
