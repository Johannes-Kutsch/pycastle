import asyncio
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from ..agents.output_protocol import AgentOutput, AgentRole, process_stream_from_events
from ..config import Config, resolve_logs_dir
from ..display.status_display import PlainStatusDisplay
from ..errors import (
    AgentCredentialFailureError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle_agent_runtime.agent_log import AgentInvocationLog
from ..services.flag_profiles import AgentToolPolicyGroup
from .docker_session import DockerSession
from ..errors import DockerError
from .preflight_failure_interpreter import PreflightCommandFailure
from ..services.agent_service import AgentService
from ..session import RunKind
from ._logged_line_stream import stream_logged_work_lines


class ContainerRunner:
    def __init__(
        self,
        name: str,
        session: DockerSession,
        model: str = "",
        effort: str = "",
        status_display=None,
        *,
        cfg: Config,
        service: AgentService | None = None,
    ) -> None:
        self.name = name
        self._session = session
        self.model = model
        self.effort = effort
        self._cfg = cfg
        self._logs_dir = resolve_logs_dir(cfg)
        self._service = service
        self._invocation_log = AgentInvocationLog()
        self._logical_session = self._invocation_log.start_logical_session(
            agent_name=name,
            effective_logs_dir=self._logs_dir,
        )
        self._status_display = (
            status_display if status_display is not None else PlainStatusDisplay()
        )

    @property
    def log_path(self) -> Path:
        return self._logical_session.log_path

    async def setup(self, git_name: str, git_email: str, work_body: str = "") -> None:
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._session.__enter__)
        await loop.run_in_executor(
            None,
            self._session.exec_simple,
            f"git config --global user.name {shlex.quote(git_name)}",
        )
        await loop.run_in_executor(
            None,
            self._session.exec_simple,
            f"git config --global user.email {shlex.quote(git_email)}",
        )
        await loop.run_in_executor(
            None,
            self._session.exec_simple,
            "pip install -e '.[dev]' || pip install -r requirements.txt",
        )

    async def preflight(
        self,
        checks: list[tuple[str, str]],
    ) -> list[PreflightCommandFailure]:
        loop = asyncio.get_running_loop()
        failures: list[PreflightCommandFailure] = []
        total = len(checks)
        for i, (check_name, command) in enumerate(checks, 1):
            self._status_display.update_phase(
                self.name, f"Running {check_name} ({i}/{total})"
            )
            try:
                await loop.run_in_executor(None, self._session.exec_simple, command)
            except DockerError as exc:
                failures.append(
                    PreflightCommandFailure(
                        check_name=check_name,
                        command=command,
                        output=str(exc),
                    )
                )
        return failures

    async def work(
        self,
        role: AgentRole,
        prompt: str,
        *,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> AgentOutput:
        self._status_display.update_phase(self.name, "Work")
        if self._service is None:
            raise RuntimeError("ContainerRunner.work requires an agent service")
        loop = asyncio.get_running_loop()

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        def on_tokens(tokens: int) -> None:
            self._status_display.update_tokens(self.name, tokens)

        return await loop.run_in_executor(
            None,
            lambda: self._run_streaming(
                role,
                prompt,
                on_turn,
                on_tokens,
                run_kind,
                session_uuid,
                on_provider_session_id,
            ),
        )

    async def work_text(
        self,
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy: AgentToolPolicyGroup = AgentToolPolicyGroup.FULL,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> str:
        self._status_display.update_phase(self.name, "Work")
        if self._service is None:
            raise RuntimeError("ContainerRunner.work_text requires an agent service")
        loop = asyncio.get_running_loop()

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        def on_tokens(tokens: int) -> None:
            self._status_display.update_tokens(self.name, tokens)

        return await loop.run_in_executor(
            None,
            lambda: self._run_streaming_text(
                role,
                prompt,
                tool_policy,
                on_turn,
                on_tokens,
                run_kind,
                session_uuid,
                on_provider_session_id,
            ),
        )

    def _run_streaming(
        self,
        role: AgentRole,
        prompt: str,
        on_turn: Callable[[str], None],
        on_tokens: Callable[[int], None] | None = None,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> AgentOutput:
        service = self._service
        if service is None:
            raise RuntimeError("ContainerRunner.work requires an agent service")
        self._session.write_file(prompt, "/tmp/.pycastle_prompt")
        command = service.build_command(
            role=role,
            model=self.model,
            effort=self.effort,
            run_kind=run_kind,
            session_uuid=session_uuid,
        )
        logged_lines = stream_logged_work_lines(
            self._session.exec_stream(command),
            logical_session=self._logical_session,
            role=role,
            run_kind=run_kind,
            session_uuid=session_uuid,
            prompt=prompt,
            idle_timeout=self._cfg.idle_timeout,
            on_chunk=lambda: self._status_display.reset_idle_timer(self.name),
        )
        parsed_events = service.run(
            logged_lines,
            on_provider_session_id=on_provider_session_id,
        )

        try:
            return process_stream_from_events(
                parsed_events,
                on_turn,
                role,
                on_tokens,
                provider=service.name,
            )
        finally:
            try:
                self._session.exec_simple("rm -f /tmp/.pycastle_prompt")
            except Exception:
                pass

    def _run_streaming_text(
        self,
        role: AgentRole,
        prompt: str,
        tool_policy: AgentToolPolicyGroup,
        on_turn: Callable[[str], None],
        on_tokens: Callable[[int], None] | None = None,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> str:
        service = self._service
        if service is None:
            raise RuntimeError("ContainerRunner.work_text requires an agent service")
        self._session.write_file(prompt, "/tmp/.pycastle_prompt")
        command = cast(Any, service).build_command(
            role=role,
            model=self.model,
            effort=self.effort,
            run_kind=run_kind,
            session_uuid=session_uuid,
            tool_policy=tool_policy,
        )
        logged_lines = stream_logged_work_lines(
            self._session.exec_stream(command),
            logical_session=self._logical_session,
            role=role,
            run_kind=run_kind,
            session_uuid=session_uuid,
            prompt=prompt,
            idle_timeout=self._cfg.idle_timeout,
            on_chunk=lambda: self._status_display.reset_idle_timer(self.name),
        )
        parsed_events = service.run(
            logged_lines,
            on_provider_session_id=on_provider_session_id,
        )

        try:
            return _result_text_from_events(
                parsed_events,
                on_turn,
                on_tokens,
                provider=service.name,
            )
        finally:
            try:
                self._session.exec_simple("rm -f /tmp/.pycastle_prompt")
            except Exception:
                pass


def _result_text_from_events(
    events,
    on_turn: Callable[[str], None],
    on_tokens: Callable[[int], None] | None = None,
    *,
    provider: str,
) -> str:
    from ..services.agent_service import (
        AssistantTurn,
        CredentialFailure,
        HardError,
        PromptTokens,
        Result,
        TransientError,
        UnsupportedTokens,
        UsageLimit,
    )

    result_text: str | None = None
    collected_turns: list[str] = []
    for event in events:
        if isinstance(event, UsageLimit):
            raise UsageLimitError(
                reset_time=event.reset_time,
                raw_message=event.raw_message,
                provider=provider,
                is_permanent=event.is_permanent,
            )
        if isinstance(event, TransientError):
            raise TransientAgentError(
                message=event.raw_message,
                status_code=event.status_code,
            )
        if isinstance(event, HardError):
            raise HardAgentError(
                message=event.raw_message,
                status_code=event.status_code,
                service_name=provider,
                classification=event.classification,
                observations=event.observations,
            )
        if isinstance(event, CredentialFailure):
            raise AgentCredentialFailureError(
                message=event.raw_message,
                status_code=event.status_code,
                service_name=event.service_name,
                classification=event.classification,
                observations=event.source_observations,
            )
        if isinstance(event, PromptTokens):
            if on_tokens is not None:
                on_tokens(event.count)
            continue
        if isinstance(event, UnsupportedTokens):
            continue
        if isinstance(event, AssistantTurn):
            on_turn(event.text)
            collected_turns.append(event.text)
            continue
        if isinstance(event, Result):
            result_text = event.text
            break
    if result_text is not None:
        return result_text
    return "\n".join(collected_turns)
