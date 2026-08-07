import asyncio
import contextlib
import dataclasses
import shlex
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import agent_runtime
import agent_runtime.runtime
from agent_runtime import (
    HardAgentError,
    InvocationFailureKind,
    ProviderInvocationFailure,
    ProviderInvocationRequest,
    ProviderInvocationResult,
    consume_provider_stdout_lines,
)
from agent_runtime.contracts import ToolAccess, ToolPolicyProfile
from agent_runtime.errors import ProviderUnavailableError as _ARProviderUnavailableError
from agent_runtime.errors import ProviderUnavailableReason
from agent_runtime.errors import UsageLimitError as _ARUsageLimitError
from agent_runtime.runtime import (
    Completed,
    Continuation,
    NewSessionRunRequest,
    ProviderUnavailable,
    ResumedSessionRunRequest,
    TimedOut,
    UsageLimited,
)
from agent_runtime.types import ProviderSelection

from pycastle.agents.output_protocol import AgentOutput, AgentRole, extract_output
from pycastle.config import Config, resolve_logs_dir
from pycastle.display.status_display import (
    WORK_PHASE,
    PlainStatusDisplay,
    StatusDisplay,
)
from pycastle.errors import (
    AgentTimeoutError,
    DockerError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.execution_contracts import WorkSessionState
from pycastle.infrastructure.agent_invocation_log import AgentInvocationLog
from pycastle.infrastructure.docker_session import DockerSession
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.services.runtime_services import AgentService
from pycastle.services.runtime_services import ToolPolicy as ServiceToolPolicy
from pycastle.session import RunKind

_DEFAULT_PROVIDER_EFFORT = "medium"


@dataclasses.dataclass
class _RunWithRuntimeRequest:
    role: AgentRole
    prompt: str
    tool_policy: ServiceToolPolicy
    on_turn: Callable[[str], None]
    on_tokens: Callable[[int], None] | None
    run_kind: RunKind
    session_uuid: str | None
    on_provider_session_id: Callable[[str], None] | None
    text_parsing: bool


@dataclasses.dataclass
class _OutcomeProcessingContext:
    role: AgentRole
    service: Any  # AgentService; avoid circular import
    on_tokens: Callable[[int], None] | None
    text_parsing: bool
    observed_provider_session_id: list[str | None]
    record_provider_session_id: Callable[[str], None]


class _DockerBackedProviderInvocationAdapter:
    """Adapter that executes provider invocations through docker session streams."""

    def __init__(self, session: DockerSession) -> None:
        self._session = session

    def execute(
        self,
        request: ProviderInvocationRequest,
        argv_transform: (
            Callable[[tuple[str, ...], Path, dict[str, str]], tuple[str, ...]] | None
        ) = None,
    ) -> ProviderInvocationResult | ProviderInvocationFailure:
        worktree = request.worktree
        environment = dict(request.environment)
        requested_command = request.command
        requested_argv = request.argv
        use_shell = not (request.prefer_argv and requested_argv)

        if argv_transform is not None:
            requested_command = ""
            requested_argv = argv_transform(requested_argv, worktree, environment)
            use_shell = False

        if use_shell:
            command = requested_command
        else:
            command = " ".join(shlex.quote(part) for part in requested_argv)
        if not command:
            raise RuntimeError("No provider command available.")

        stdout_lines: list[str] = []
        for chunk in self._session.exec_stream(command):
            if not isinstance(chunk, bytes):
                continue
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                consume_provider_stdout_lines(
                    request.output_hooks.reduce_output,
                    [line],
                )
                stdout_lines.append(line)

        return _process_execution_output(request, stdout_lines)


def _process_execution_output(
    request: ProviderInvocationRequest,
    stdout_lines: list[str],
) -> ProviderInvocationResult | ProviderInvocationFailure:
    try:
        output, usage = request.output_hooks.reduce_output(stdout_lines)
    except Exception as exc:
        if isinstance(exc, (_ARUsageLimitError, _ARProviderUnavailableError)):
            provider_session_id: str | None = None
            if request.output_hooks.extract_provider_session_id is not None:
                provider_session_id = request.output_hooks.extract_provider_session_id(
                    stdout_lines
                )
            if isinstance(exc, _ARUsageLimitError):
                return ProviderInvocationFailure(
                    kind=InvocationFailureKind.USAGE_LIMITED,
                    detail=exc.raw_message or str(exc),
                    stdout_lines=tuple(stdout_lines),
                    provider_session_id=provider_session_id,
                    usage=exc.usage,
                    reset_time=exc.reset_time,
                    is_permanent=exc.is_permanent,
                )
            return ProviderInvocationFailure(
                kind=InvocationFailureKind.PROVIDER_UNAVAILABLE,
                detail=str(exc),
                stdout_lines=tuple(stdout_lines),
                provider_session_id=provider_session_id,
                usage=exc.usage,
                reset_time=None,
                provider_unavailable_reason=exc.reason,
            )
        raise

    provider_session_id = None
    if request.output_hooks.extract_provider_session_id is not None:
        provider_session_id = request.output_hooks.extract_provider_session_id(
            stdout_lines
        )

    if not output.strip():
        error = HardAgentError(
            "Provider subprocess completed without producing output."
        )
        error.provider_session_id = provider_session_id
        raise error

    return ProviderInvocationResult(
        output=output,
        usage=usage,
        stdout_lines=tuple(stdout_lines),
        provider_session_id=provider_session_id,
    )



@dataclasses.dataclass
class _ContainerRunnerConfig:
    cfg: Config
    model: str = ""
    effort: str = ""
    status_display: StatusDisplay | None = None
    service: AgentService | None = None
    runtime_client: Any | None = None  # agent_runtime.RuntimeClient
    mount_path: Path | None = None


class ContainerRunner:
    def __init__(
        self,
        name: str,
        session: DockerSession,
        config: _ContainerRunnerConfig,
    ) -> None:
        self.name = name
        self._session = session
        self.model = config.model
        self.effort = config.effort
        self._cfg = config.cfg
        self._logs_dir = resolve_logs_dir(config.cfg)
        self._service = config.service
        self._runtime_client = config.runtime_client
        self._mount_path = config.mount_path
        self._invocation_log = AgentInvocationLog()
        self._logical_session = self._invocation_log.start_logical_session(
            agent_name=name,
            effective_logs_dir=self._logs_dir,
        )
        self._status_display = (
            config.status_display
            if config.status_display is not None
            else PlainStatusDisplay()
        )
        self._current_work_invocation: Any | None = None

    @property
    def log_path(self) -> Path:
        return self._logical_session.log_path

    def provider_argv_transform(
        self,
    ) -> Callable[
        [tuple[str, ...], Path, Mapping[str, str]],
        tuple[str, ...],
    ]:
        session = cast("Any", self._session)
        container = getattr(session, "_active_container", None)
        if container is None:
            container = session.__dict__.get("_container")
        if container is None or not hasattr(container, "id"):
            raise RuntimeError("ContainerRunner requires an active container")
        container_id = str(container.id)

        def _transform(
            argv: tuple[str, ...],
            invocation_dir: Path,
            env: Mapping[str, str],
        ) -> tuple[str, ...]:
            del invocation_dir
            transformed: list[str] = ["docker", "exec", "-i"]
            for key, value in env.items():
                if key == "OPENCODE_CONFIG_CONTENT" or key.endswith(("_TOKEN", "_KEY")):
                    transformed.extend(["-e", f"{key}={value}"])
            transformed.append(container_id)
            transformed.extend(argv)
            return tuple(transformed)

        return _transform

    def get_runtime_client(self) -> Any:  # noqa: ANN401  # same rationale as _get_runtime_client
        return self._get_runtime_client()

    def exec_command(self, command: str) -> str:
        return self._session.exec_simple(command)

    def on_live_output(self, event: agent_runtime.AgentEvent) -> bool:
        """Dispatch a live output event; returns True if a provider chunk was appended."""
        self._status_display.reset_idle_timer(self.name)
        raw_provider_output = getattr(event, "raw_provider_output", "")
        logged = False
        if raw_provider_output and self._current_work_invocation is not None:
            chunk = raw_provider_output
            if not chunk.endswith("\n"):
                chunk += "\n"
            self._current_work_invocation.append_provider_chunk(chunk.encode())
            logged = True
        if getattr(event, "type", None) == "agent_message":
            display_message = getattr(event, "display_message", "")
            if display_message:
                self._status_display.print(self.name, display_message)
        return logged

    @contextlib.contextmanager
    def open_work_invocation(
        self,
        *,
        role: AgentRole,
        run_kind: RunKind,
        session_uuid: str | None,
        prompt: str,
    ) -> Iterator[Any]:  # yields whatever _logical_session.open_work_invocation yields
        with self._logical_session.open_work_invocation(
            role=role,
            run_kind=run_kind,
            session_uuid=session_uuid,
            prompt=prompt,
        ) as work_invocation:
            self._current_work_invocation = work_invocation
            try:
                yield work_invocation
            finally:
                self._current_work_invocation = None

    def append_chunk(self, chunk: str) -> None:
        if self._current_work_invocation is not None:
            encoded = chunk if chunk.endswith("\n") else f"{chunk}\n"
            self._current_work_invocation.append_provider_chunk(encoded.encode())

    async def setup(self, git_name: str, git_email: str) -> None:
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
        self._status_display.update_phase(self.name, WORK_PHASE)
        if self._service is None:
            raise RuntimeError("ContainerRunner.work requires an agent service")

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        def on_tokens(tokens: int) -> None:
            self._status_display.update_tokens(self.name, tokens)

        request = _RunWithRuntimeRequest(
            role=role,
            prompt=prompt,
            tool_policy=ServiceToolPolicy.FULL,
            on_turn=on_turn,
            on_tokens=on_tokens,
            run_kind=run_kind,
            session_uuid=session_uuid,
            on_provider_session_id=on_provider_session_id,
            text_parsing=False,
        )
        return cast(
            "AgentOutput",
            await self._run_with_runtime(request),
        )

    async def work_text(
        self,
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy: ServiceToolPolicy = ServiceToolPolicy.FULL,
        session: WorkSessionState | None = None,
    ) -> str:
        _session = session or WorkSessionState()
        self._status_display.update_phase(self.name, WORK_PHASE)
        if self._service is None:
            raise RuntimeError("ContainerRunner.work_text requires an agent service")

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        def on_tokens(tokens: int) -> None:
            self._status_display.update_tokens(self.name, tokens)

        return cast(
            "str",
            await self._run_with_runtime(
                _RunWithRuntimeRequest(
                    role=role,
                    prompt=prompt,
                    tool_policy=tool_policy,
                    on_turn=on_turn,
                    on_tokens=on_tokens,
                    run_kind=_session.run_kind,
                    session_uuid=_session.session_uuid,
                    on_provider_session_id=_session.on_provider_session_id,
                    text_parsing=True,
                )
            ),
        )

    async def _run_with_runtime(
        self, request: _RunWithRuntimeRequest
    ) -> AgentOutput | str:
        service = self._service
        if service is None:
            raise RuntimeError("ContainerRunner requires an agent service")
        observed_provider_session_id: list[str | None] = [request.session_uuid]

        def _record_provider_session_id(provider_session_id: str) -> None:
            observed_provider_session_id[0] = provider_session_id
            if request.on_provider_session_id is not None:
                request.on_provider_session_id(provider_session_id)

        logged_lines = [False]

        def _on_live_output(event: agent_runtime.AgentEvent) -> None:
            self._status_display.reset_idle_timer(self.name)
            display_message = getattr(event, "display_message", "")
            if display_message:
                request.on_turn(display_message)
            raw = getattr(event, "raw_provider_output", "")
            if raw and self._current_work_invocation is not None:
                self.append_chunk(raw)
                logged_lines[0] = True

        runtime_request = self._build_runtime_request(
            prompt=request.prompt,
            run_kind=request.run_kind,
            session_uuid=request.session_uuid,
            tool_policy=request.tool_policy,
            on_live_output=_on_live_output,
        )
        runtime = self._get_runtime_client()

        with self.open_work_invocation(
            role=request.role,
            run_kind=request.run_kind,
            session_uuid=request.session_uuid,
            prompt=request.prompt,
        ):
            if request.run_kind is RunKind.FRESH:
                outcome = await runtime.run_new_session(runtime_request)
            else:
                outcome = await runtime.run_resumed_session(runtime_request)
            if not logged_lines[0] and outcome.result.output:
                self.append_chunk(outcome.result.output)

        ctx = _OutcomeProcessingContext(
            role=request.role,
            service=service,
            on_tokens=request.on_tokens,
            text_parsing=request.text_parsing,
            observed_provider_session_id=observed_provider_session_id,
            record_provider_session_id=_record_provider_session_id,
        )
        return self._process_runtime_outcome(outcome, ctx)

    def _process_runtime_outcome(
        self,
        outcome: Any,  # noqa: ANN401  # opaque agent_runtime outcome; no shared protocol
        ctx: _OutcomeProcessingContext,
    ) -> AgentOutput | str:
        outcome_kind = outcome.kind
        if isinstance(outcome_kind, Completed):
            return self._handle_completed_outcome(outcome, ctx)
        self._logical_session.record_provider_session_id(
            ctx.observed_provider_session_id[0]
        )
        if isinstance(outcome_kind, UsageLimited):
            raise UsageLimitError(
                reset_time=outcome_kind.reset_time,
                provider=outcome.result.selected.service,
            )
        if isinstance(outcome_kind, ProviderUnavailable):
            if outcome_kind.reason is ProviderUnavailableReason.TRANSIENT_API_ERROR:
                raise TransientAgentError(message=outcome_kind.detail)
            raise UsageLimitError(
                provider=ctx.service.name,
                raw_message=outcome_kind.detail,
            )
        if isinstance(outcome_kind, TimedOut):
            raise AgentTimeoutError(
                "Provider timed out",
                role_value=ctx.role.value,
            )
        raise RuntimeError("Unexpected runtime outcome kind")

    def _handle_completed_outcome(
        self,
        outcome: Any,  # noqa: ANN401  # opaque agent_runtime outcome
        ctx: _OutcomeProcessingContext,
    ) -> AgentOutput | str:
        if outcome.result.continuation is not None:
            continuation_session_id = (
                outcome.result.continuation.provider_session_id
                if hasattr(outcome.result.continuation, "provider_session_id")
                else None
            )
            if continuation_session_id is None:
                continuation_session_id = getattr(
                    outcome.result.continuation,
                    "serialized",
                    None,
                )
            if continuation_session_id is not None:
                ctx.observed_provider_session_id[0] = continuation_session_id
                ctx.record_provider_session_id(continuation_session_id)
        self._logical_session.record_provider_session_id(
            ctx.observed_provider_session_id[0]
        )
        usage = outcome.result.usage
        if usage is not None:
            tokens = (
                (usage.input_tokens or 0)
                + (usage.cache_creation_input_tokens or 0)
                + (usage.cache_read_input_tokens or 0)
            )
            if tokens:
                ctx.on_tokens and ctx.on_tokens(tokens)
        if ctx.text_parsing:
            return outcome.result.output
        return extract_output(outcome.result.output, ctx.role)

    def _get_runtime_client(self) -> Any:  # noqa: ANN401  # returns injected client or agent_runtime.RuntimeClient; no shared protocol
        if self._runtime_client is not None:
            return self._runtime_client
        if shutil.which("docker") is None:
            return agent_runtime.RuntimeClient(
                provider_invocation_adapter=_DockerBackedProviderInvocationAdapter(
                    self._session
                )
            )
        return agent_runtime.RuntimeClient()

    def _build_runtime_request(
        self,
        *,
        prompt: str,
        run_kind: RunKind,
        session_uuid: str | None,
        on_live_output: Callable[[Any], None],
        tool_policy: ServiceToolPolicy | None = None,
    ) -> NewSessionRunRequest | ResumedSessionRunRequest:
        service_name = self._service.name if self._service is not None else "claude"
        invocation_dir = (
            self._mount_path if self._mount_path is not None else Path.cwd()
        )
        tool_access = _coerce_tool_access(
            tool_policy if tool_policy is not None else ServiceToolPolicy.FULL,
            workspace=invocation_dir,
        )
        if run_kind == RunKind.RESUME:
            if session_uuid is None:
                raise RuntimeError(
                    "ContainerRunner cannot resume without a provider session id"
                )
            continuation = Continuation(
                selected_service=service_name,
                selected_model=self.model or self._default_model(),
                selected_effort=self.effort or _DEFAULT_PROVIDER_EFFORT,
                tool_access=tool_access,
                serialized=None,
                provider_resume_state={
                    "provider_session_id": session_uuid,
                },
            )
            return ResumedSessionRunRequest(
                prompt=prompt,
                invocation_dir=invocation_dir,
                continuation=continuation,
                provider_auth=None,
                session_store=invocation_dir,
                timeout_seconds=self._cfg.idle_timeout,
                on_live_output=on_live_output,
                argv_transform=self.provider_argv_transform(),
            )
        return NewSessionRunRequest(
            prompt=prompt,
            invocation_dir=invocation_dir,
            provider_selection=ProviderSelection(
                service=service_name,
                model=self.model or self._default_model(),
                effort=self.effort or _DEFAULT_PROVIDER_EFFORT,
            ),
            tool_access=tool_access,
            session_store=invocation_dir,
            on_live_output=on_live_output,
            timeout_seconds=self._cfg.idle_timeout,
            argv_transform=self.provider_argv_transform(),
        )

    def _default_model(self) -> str:
        if self._service is None:
            return "gpt-5.5"
        valid_models = self._service.valid_models()
        for candidate in ("gpt-5.5", "gpt-5.4", "haiku", "opus", "sonnet"):
            if candidate in valid_models:
                return candidate
        if not valid_models:
            return "gpt-5.5"
        return min(valid_models)


def _coerce_tool_access(
    tool_policy: ServiceToolPolicy,
    *,
    workspace: Path | None = None,
) -> ToolAccess:
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    if tool_policy is ServiceToolPolicy.RESTRICTED:
        allowed_tools = ("Read", "Glob")
    elif tool_policy is ServiceToolPolicy.PARTIAL:
        disallowed_tools = ("Edit", "Write", "NotebookEdit")

    tool_policy_profile = ToolPolicyProfile(
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        strict_mcp_config=True,
    )

    return ToolAccess(
        kind="workspace_backed",
        workspace=workspace,
        tool_policy=tool_policy_profile,
    )
