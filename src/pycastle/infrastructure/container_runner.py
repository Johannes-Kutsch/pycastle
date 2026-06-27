import asyncio
import shlex
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import agent_runtime.runtime
from agent_runtime.contracts import ToolAccess, ToolPolicyProfile
from agent_runtime import _provider_invocation, _session_backed_provider_execution
from agent_runtime.errors import ProviderUnavailableReason
from agent_runtime._provider_invocation import (
    ProviderInvocationFailure,
    ProviderInvocationResult,
    ProviderInvocationRequest,
)
from agent_runtime.runtime import (
    NewSessionRunRequest,
    ProviderUnavailable,
    ResumedSessionRunRequest,
    Completed,
    UsageLimited,
    TimedOut,
    Continuation,
)
from agent_runtime.types import ProviderSelection

from ..agents.output_protocol import AgentOutput, AgentRole, extract_output
from ..config import Config, resolve_logs_dir
from ..display.status_display import PlainStatusDisplay
from .agent_invocation_log import AgentInvocationLog
from ..services.runtime_services import AgentService, ToolPolicy as ServiceToolPolicy
from .docker_session import DockerSession
from ..errors import (
    AgentTimeoutError,
    DockerError,
    TransientAgentError,
    UsageLimitError,
)
from .preflight_failure_interpreter import PreflightCommandFailure
from ..session import RunKind


_DEFAULT_PROVIDER_EFFORT = "medium"


class _DockerBackedProviderInvocationAdapter:
    """Adapter that executes provider invocations through docker session streams."""

    def __init__(self, session: Any) -> None:
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
                _provider_invocation._consume_new_stdout_lines(
                    request.output_hooks.reduce_output,
                    [line],
                )
                stdout_lines.append(line)

        try:
            output, usage = request.output_hooks.reduce_output(stdout_lines)
        except Exception as exc:
            if isinstance(
                exc,
                (
                    _provider_invocation.UsageLimitError,
                    _provider_invocation.ProviderUnavailableError,
                ),
            ):
                provider_session_id: str | None = None
                if request.output_hooks.extract_provider_session_id is not None:
                    provider_session_id = (
                        request.output_hooks.extract_provider_session_id(stdout_lines)
                    )
                return _provider_invocation._provider_invocation_failure_from_error(
                    exc,
                    stdout_lines=tuple(stdout_lines),
                    provider_session_id=provider_session_id,
                )
            raise

        provider_session_id = None
        if request.output_hooks.extract_provider_session_id is not None:
            provider_session_id = request.output_hooks.extract_provider_session_id(
                stdout_lines
            )

        if not output.strip():
            error = _provider_invocation.HardAgentError(
                "Provider subprocess completed without producing output."
            )
            setattr(error, "provider_session_id", provider_session_id)
            raise error

        return ProviderInvocationResult(
            output=output,
            usage=usage,
            stdout_lines=tuple(stdout_lines),
            provider_session_id=provider_session_id,
        )


class _DockerlessRuntimeClient:
    """Fallback runtime client used when host docker CLI is unavailable."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._invocation_adapter = _DockerBackedProviderInvocationAdapter(session)

    async def run_new_session(self, request: NewSessionRunRequest) -> Any:
        return _session_backed_provider_execution._run_builtin_new_session(
            request,
            provider_invocation_adapter=self._invocation_adapter,
            on_live_output=request.on_live_output,
        )

    async def run_resumed_session(self, request: ResumedSessionRunRequest) -> Any:
        return _session_backed_provider_execution._run_builtin_resumed_session(
            request,
            provider_invocation_adapter=self._invocation_adapter,
            on_live_output=request.on_live_output,
        )


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
        runtime_client: Any | None = None,
        mount_path: Path | None = None,
    ) -> None:
        self.name = name
        self._session = session
        self.model = model
        self.effort = effort
        self._cfg = cfg
        self._logs_dir = resolve_logs_dir(cfg)
        self._service = service
        self._runtime_client = runtime_client
        self._mount_path = mount_path
        self._invocation_log = AgentInvocationLog()
        self._logical_session = self._invocation_log.start_logical_session(
            agent_name=name,
            effective_logs_dir=self._logs_dir,
        )
        self._status_display = (
            status_display if status_display is not None else PlainStatusDisplay()
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
        session = cast(Any, self._session)
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
                if (
                    key == "OPENCODE_CONFIG_CONTENT"
                    or key.endswith("_TOKEN")
                    or key.endswith("_KEY")
                ):
                    transformed.extend(["-e", f"{key}={value}"])
            transformed.append(container_id)
            transformed.extend(argv)
            return tuple(transformed)

        return _transform

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

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        def on_tokens(tokens: int) -> None:
            self._status_display.update_tokens(self.name, tokens)

        return cast(
            AgentOutput,
            await self._run_with_runtime(
                role=role,
                prompt=prompt,
                tool_policy=ServiceToolPolicy.FULL,
                on_turn=on_turn,
                on_tokens=on_tokens,
                run_kind=run_kind,
                session_uuid=session_uuid,
                on_provider_session_id=on_provider_session_id,
                text_parsing=False,
            ),
        )

    async def work_text(
        self,
        prompt: str,
        *,
        role: AgentRole = AgentRole.IMPLEMENTER,
        tool_policy: ServiceToolPolicy = ServiceToolPolicy.FULL,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> str:
        self._status_display.update_phase(self.name, "Work")
        if self._service is None:
            raise RuntimeError("ContainerRunner.work_text requires an agent service")

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        def on_tokens(tokens: int) -> None:
            self._status_display.update_tokens(self.name, tokens)

        return cast(
            str,
            await self._run_with_runtime(
                role=role,
                prompt=prompt,
                tool_policy=tool_policy,
                on_turn=on_turn,
                on_tokens=on_tokens,
                run_kind=run_kind,
                session_uuid=session_uuid,
                on_provider_session_id=on_provider_session_id,
                text_parsing=True,
            ),
        )

    async def _run_with_runtime(
        self,
        role: AgentRole,
        prompt: str,
        tool_policy: ServiceToolPolicy,
        on_turn: Callable[[str], None],
        on_tokens: Callable[[int], None] | None = None,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        on_provider_session_id: Callable[[str], None] | None = None,
        text_parsing: bool = False,
    ) -> AgentOutput | str:
        service = self._service
        if service is None:
            raise RuntimeError("ContainerRunner requires an agent service")
        observed_provider_session_id: list[str | None] = [session_uuid]

        def _record_provider_session_id(provider_session_id: str) -> None:
            observed_provider_session_id[0] = provider_session_id
            if on_provider_session_id is not None:
                on_provider_session_id(provider_session_id)

        logged_lines = [False]

        def _on_live_output(event: Any) -> None:
            self._status_display.reset_idle_timer(self.name)
            display_message = getattr(event, "display_message", "")
            if display_message:
                on_turn(display_message)
            raw_provider_output = getattr(event, "raw_provider_output", "")
            if raw_provider_output and self._current_work_invocation is not None:
                chunk = raw_provider_output
                if not chunk.endswith("\n"):
                    chunk += "\n"
                self._current_work_invocation.append_provider_chunk(chunk.encode())
                logged_lines[0] = True

        runtime_request = self._build_runtime_request(
            prompt=prompt,
            run_kind=run_kind,
            session_uuid=session_uuid,
            tool_policy=tool_policy,
            on_live_output=_on_live_output,
        )
        runtime = self._get_runtime_client()

        with self._logical_session.open_work_invocation(
            role=role,
            run_kind=run_kind,
            session_uuid=session_uuid,
            prompt=prompt,
        ) as work_invocation:
            self._current_work_invocation = work_invocation
            if run_kind is RunKind.FRESH:
                outcome = await runtime.run_new_session(runtime_request)
            else:
                outcome = await runtime.run_resumed_session(runtime_request)
            if not logged_lines[0] and outcome.result.output:
                work_invocation_output = (
                    outcome.result.output
                    if outcome.result.output.endswith("\n")
                    else f"{outcome.result.output}\n"
                )
                work_invocation.append_provider_chunk(work_invocation_output.encode())
            self._current_work_invocation = None

        try:
            outcome_kind = outcome.kind
            if isinstance(outcome_kind, Completed):
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
                        observed_provider_session_id[0] = continuation_session_id
                        _record_provider_session_id(continuation_session_id)
                self._logical_session.record_provider_session_id(
                    observed_provider_session_id[0]
                )
                usage = outcome.result.usage
                if usage is not None:
                    tokens = (
                        (usage.input_tokens or 0)
                        + (usage.cache_creation_input_tokens or 0)
                        + (usage.cache_read_input_tokens or 0)
                    )
                    if tokens:
                        on_tokens and on_tokens(tokens)
                if text_parsing:
                    return outcome.result.output
                return extract_output(outcome.result.output, role)
            if isinstance(outcome_kind, UsageLimited):
                self._logical_session.record_provider_session_id(
                    observed_provider_session_id[0]
                )
                raise UsageLimitError(
                    reset_time=outcome_kind.reset_time,
                    provider=outcome.result.selected.service,
                )
            if isinstance(outcome_kind, ProviderUnavailable):
                self._logical_session.record_provider_session_id(
                    observed_provider_session_id[0]
                )
                if outcome_kind.reason is ProviderUnavailableReason.TRANSIENT_API_ERROR:
                    raise TransientAgentError(message=outcome_kind.detail)
                raise UsageLimitError(
                    provider=service.name,
                    raw_message=outcome_kind.detail,
                )
            if isinstance(outcome_kind, TimedOut):
                self._logical_session.record_provider_session_id(
                    observed_provider_session_id[0]
                )
                raise AgentTimeoutError(
                    "Provider timed out",
                    role_value=role.value,
                )
            self._logical_session.record_provider_session_id(
                observed_provider_session_id[0]
            )
            raise RuntimeError("Unexpected runtime outcome kind")
        finally:
            self._current_work_invocation = None

    def _get_runtime_client(self) -> Any:
        if self._runtime_client is not None:
            return self._runtime_client
        if shutil.which("docker") is None:
            return _DockerlessRuntimeClient(self._session)
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
        try:
            valid_models = self._service.valid_models()
        except Exception:
            return "gpt-5.5"
        for candidate in ("gpt-5.5", "gpt-5.4", "haiku", "opus", "sonnet"):
            if candidate in valid_models:
                return candidate
        if not valid_models:
            return "gpt-5.5"
        return sorted(valid_models)[0]


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
