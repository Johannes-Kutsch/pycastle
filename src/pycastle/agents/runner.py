import asyncio
import dataclasses
import json
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Protocol

from .output_protocol import (
    AgentOutput,
    AgentOutputProtocolError,
    AgentRole,
    AgentSuccessOutput,
    FailedOutput,
)
from .result import CancellationToken
from ..config import Config, image_name_for
from ..infrastructure.container_runner import ContainerRunner
from ..infrastructure.docker_session import DockerSession, build_volume_spec
from ..infrastructure.preflight_tool_classifier import load_python_dependency_metadata
from ..errors import (
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from ..prompts.pipeline import PromptRenderer, PromptTemplate
from ..session import RoleSession, RunKind
from ..services import GitService
from ..services.agent_service import AgentService
from ..services.claude_service import ClaudeService
from ..display.status_display import ModelDisplayMetadata, PlainStatusDisplay

_CONTAINER_WORKSPACE = "/home/agent/workspace"

REPROMPT_MESSAGE = (
    "Your last response did not include the required protocol output. "
    "Please review the task requirements and try again, making sure to "
    "include the required output tag."
)


def _stage_key_for_role(role: AgentRole) -> str | None:
    mapping = {
        AgentRole.PLANNER: "plan",
        AgentRole.IMPLEMENTER: "implement",
        AgentRole.REVIEWER: "review",
        AgentRole.MERGER: "merge",
        AgentRole.PREFLIGHT_ISSUE: "preflight_issue",
        AgentRole.IMPROVE: "improve",
        AgentRole.FAILURE_REPORT: "preflight_issue",
    }
    return mapping.get(role)


@dataclasses.dataclass
class RunRequest:
    name: str
    template: PromptTemplate
    mount_path: Path
    role: AgentRole = AgentRole.IMPLEMENTER
    scope_args: dict[str, str] | None = None
    model: str = ""
    effort: str = ""
    service: str = ""
    stage: str = ""
    token: CancellationToken | None = None
    status_display: Any = None
    issue_title: str = ""
    work_body: str = ""
    send_role_prompt_on_resume: bool = False
    session_namespace: str = ""


async def translate_run_outcome(
    inner: Coroutine[Any, Any, AgentOutput], request: RunRequest
) -> AgentSuccessOutput:
    try:
        output = await inner
        if isinstance(output, FailedOutput):
            raise AgentFailedError(
                role_value=request.role.value,
                worktree_path=request.mount_path,
                namespace=request.session_namespace,
                failure_class=output.failure_class,
                service_name=request.service,
            )
        return output
    except AgentTimeoutError as err:
        if not err.role_value:
            err.role_value = request.role.value
            err.worktree_path = request.mount_path
        raise


class AgentRunnerProtocol(Protocol):
    async def run(self, request: RunRequest) -> AgentSuccessOutput: ...

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
        status_display=None,
        work_body: str = "",
    ) -> list[tuple[str, str, str]]: ...


class AgentRunner:
    def __init__(
        self,
        env: dict[str, str],
        cfg: Config,
        git_service: GitService,
        docker_client=None,
        service_registry: dict[str, AgentService] | None = None,
    ) -> None:
        self._env = env
        self._cfg = cfg
        self._git_service = git_service
        self._docker_client = docker_client
        self._service_registry = service_registry or {"claude": ClaudeService()}
        self._renderer = PromptRenderer(cfg)

    def _resolve_service(self, service_name: str = "") -> AgentService:
        resolved_name = service_name.strip()
        if not resolved_name:
            raise ValueError("Agent dispatch requires an explicit resolved service")
        service = self._service_registry.get(resolved_name)
        if service is not None:
            return service
        raise ValueError(f"Unknown agent service {resolved_name!r}")

    def _build_session(
        self,
        mount_path: Path,
        service: AgentService,
        state_dir_relpath: str | None = None,
    ) -> DockerSession:
        volumes, auto_overlay = build_volume_spec(mount_path)
        container_env = dict(self._env)
        state_dir: str | None = None
        if state_dir_relpath is not None:
            state_dir = f"{_CONTAINER_WORKSPACE}/{state_dir_relpath}"
        container_env.update(service.build_env(state_dir))
        return DockerSession(
            volumes=volumes,
            container_env=container_env,
            image_name=image_name_for(self._cfg.docker_image_name, service.name),
            cfg=self._cfg,
            docker_client=self._docker_client,
            auto_overlay=auto_overlay,
        )

    def _build_preflight_session(self, mount_path: Path) -> DockerSession:
        volumes, auto_overlay = build_volume_spec(mount_path)
        return DockerSession(
            volumes=volumes,
            container_env=dict(self._env),
            image_name=image_name_for(self._cfg.docker_image_name, ""),
            cfg=self._cfg,
            docker_client=self._docker_client,
            auto_overlay=auto_overlay,
        )

    def _host_codex_auth_path(self) -> Path:
        host_auth = Path.home() / ".codex" / "auth.json"
        if not host_auth.exists():
            raise HardAgentError(
                "Codex authentication missing: run `codex login` on the host.",
                status_code=401,
            )
        return host_auth

    def _seed_codex_auth(self, state_dir: Path, host_auth: Path) -> None:
        dest = state_dir / "auth.json"
        if dest.exists():
            return

        state_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(host_auth, dest)

    def _codex_thread_id_from_rollouts(self, state_dir: Path) -> str | None:
        sessions_dir = state_dir / "sessions"
        if not sessions_dir.is_dir():
            return None
        found: set[str] = set()
        for rollout in sessions_dir.rglob("rollout-*.jsonl"):
            try:
                lines = rollout.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") != "thread.started":
                    continue
                thread_id = obj.get("thread_id")
                if isinstance(thread_id, str) and thread_id.strip():
                    found.add(thread_id.strip())
        return next(iter(found)) if len(found) == 1 else None

    async def _build_prompt(
        self,
        template: PromptTemplate,
        scope_args: dict[str, str],
        container_exec: Callable[[str], Coroutine[Any, Any, str]],
        run_kind: RunKind,
        send_role_prompt_on_resume: bool,
    ) -> str:
        if run_kind == RunKind.RESUME and not send_role_prompt_on_resume:
            return await self._renderer.render(
                PromptTemplate.RESUME, {}, container_exec
            )
        return await self._renderer.render(template, scope_args, container_exec)

    async def run(self, request: RunRequest) -> AgentSuccessOutput:
        return await translate_run_outcome(self._run(request), request)

    async def _run(self, request: RunRequest) -> AgentOutput:
        from ..iteration._rows import status_row

        name = request.name
        template = request.template
        mount_path = request.mount_path
        role = request.role
        scope_args = request.scope_args or {}
        model = request.model
        effort = request.effort
        service = self._resolve_service(request.service)
        token = request.token
        status_display = request.status_display
        work_body = request.work_body

        if status_display is None:
            status_display = PlainStatusDisplay()

        _token = token if token is not None else CancellationToken()
        if _token.is_cancelled:
            raise UsageLimitError(reset_time=None, stage_key=_stage_key_for_role(role))

        session_namespace = request.session_namespace
        role_session = RoleSession(mount_path, role, session_namespace)
        session_uuid = role_session.session_uuid()
        svc_state_relpath = service.state_dir_relpath(role, session_namespace)
        state_dir = mount_path / svc_state_relpath if svc_state_relpath else None
        run_kind = (
            RunKind.RESUME
            if svc_state_relpath
            and state_dir is not None
            and service.is_resumable(state_dir)
            else RunKind.FRESH
        )
        service_session_id: str | None = session_uuid
        if service.name == "codex":
            service_session_id = None
            if run_kind == RunKind.RESUME and state_dir is not None:
                service_session_id = role_session.service_session_id(
                    "codex"
                ) or self._codex_thread_id_from_rollouts(state_dir)
                if service_session_id is not None:
                    role_session.save_service_session_id("codex", service_session_id)
                else:
                    run_kind = RunKind.FRESH
        elif service.name == "opencode" and run_kind == RunKind.RESUME:
            service_session_id = role_session.service_session_id("opencode")
            if service_session_id is None:
                run_kind = RunKind.FRESH
        host_codex_auth: Path | None = None
        if state_dir is not None and service.name == "codex":
            auth_missing_from_state_dir = not (state_dir / "auth.json").exists()
            if run_kind == RunKind.FRESH or auth_missing_from_state_dir:
                host_codex_auth = self._host_codex_auth_path()

        non_typed_retry_done = False

        color_key: int | None = None
        if role in (AgentRole.IMPLEMENTER, AgentRole.REVIEWER):
            issue_number_str = scope_args.get("ISSUE_NUMBER", "")
            if issue_number_str.isdigit():
                color_key = int(issue_number_str)

        async with status_row(
            status_display,
            name,
            kind="agent",
            must_close=False,
            work_body=work_body,
            color_key=color_key,
            model_display=ModelDisplayMetadata(
                service=service.name,
                model=model,
                effort=effort,
            ),
        ) as row:
            session = self._build_session(mount_path, service, svc_state_relpath)
            runner = ContainerRunner(
                name,
                session,
                model=model,
                effort=effort,
                status_display=status_display,
                cfg=self._cfg,
                service=service,
            )
            try:
                git_name = self._git_service.get_user_name()
                git_email = self._git_service.get_user_email()
                try:
                    await runner.setup(git_name, git_email, work_body)
                except DockerError as exc:
                    raise SetupPhaseError(role.value, str(exc)) from exc

                if run_kind == RunKind.FRESH:
                    role_session.start_fresh()

                if state_dir is not None:
                    state_dir.mkdir(parents=True, exist_ok=True)
                    if host_codex_auth is not None:
                        self._seed_codex_auth(state_dir, host_codex_auth)

                def remember_thread_id(thread_id: str) -> None:
                    nonlocal service_session_id
                    service_session_id = thread_id
                    if service.name in {"codex", "opencode"}:
                        role_session.save_service_session_id(service.name, thread_id)

                loop = asyncio.get_running_loop()

                async def container_exec(cmd: str) -> str:
                    return await loop.run_in_executor(None, session.exec_simple, cmd)

                retries_left = self._cfg.timeout_retries
                while True:
                    try:
                        prompt = await self._build_prompt(
                            template,
                            scope_args,
                            container_exec,
                            run_kind=run_kind,
                            send_role_prompt_on_resume=request.send_role_prompt_on_resume,
                        )

                        work_prompt = prompt
                        work_run_kind = run_kind
                        for _ in range(3):
                            try:
                                if service.name in {"codex", "opencode"}:
                                    output = await runner.work(
                                        role,
                                        work_prompt,
                                        run_kind=work_run_kind,
                                        session_uuid=service_session_id,
                                        on_thread_id=remember_thread_id,
                                    )
                                else:
                                    output = await runner.work(
                                        role,
                                        work_prompt,
                                        run_kind=work_run_kind,
                                        session_uuid=service_session_id,
                                    )
                                if (
                                    not isinstance(output, FailedOutput)
                                    and service_session_id is not None
                                ):
                                    role_session.save_service_session_metadata(
                                        service.name, service_session_id
                                    )
                                if isinstance(output, FailedOutput):
                                    row.close("failed", shutdown_style="error")
                                return output
                            except AgentOutputProtocolError:
                                if (
                                    service.name == "codex"
                                    and service_session_id is None
                                ):
                                    row.close("failed", shutdown_style="error")
                                    return FailedOutput(failure_class="protocol_error")
                                work_prompt = REPROMPT_MESSAGE
                                work_run_kind = RunKind.RESUME
                        row.close("failed", shutdown_style="error")
                        return FailedOutput(failure_class="protocol_error")
                    except AgentTimeoutError:
                        if retries_left <= 0:
                            raise
                        restart_num = self._cfg.timeout_retries - retries_left + 1
                        status_display.print(
                            name,
                            f"Timeout — restarting"
                            f" (attempt {restart_num}/{self._cfg.timeout_retries})",
                        )
                        retries_left -= 1
                    except UsageLimitError as err:
                        if err.stage_key is None:
                            err.stage_key = _stage_key_for_role(role)
                        if err.is_permanent and isinstance(service, ClaudeService):
                            err.account_label = service.mark_permanently_exhausted()
                        else:
                            service.mark_exhausted(err.reset_time)
                        _token.cancel()
                        raise
                    except TransientAgentError as err:
                        _token.cancel()
                        status_code_str = (
                            str(err.status_code)
                            if err.status_code is not None
                            else "no status"
                        )
                        status_display.print(
                            name,
                            f"transient API error: status {status_code_str}",
                        )
                        raise
                    except HardAgentError as err:
                        _token.cancel()
                        err.caller = name
                        raise
                    except Exception:
                        if run_kind != RunKind.RESUME:
                            raise
                        if non_typed_retry_done:
                            row.close("failed", shutdown_style="error")
                            return FailedOutput(failure_class="non_typed_crash")
                        non_typed_retry_done = True
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
        status_display=None,
        work_body: str = "",
    ) -> list[tuple[str, str, str]]:
        from ..iteration._rows import status_row

        if status_display is None:
            status_display = PlainStatusDisplay()

        git_name = self._git_service.get_user_name()
        git_email = self._git_service.get_user_email()
        python_dependency_metadata = load_python_dependency_metadata(mount_path)
        async with status_row(
            status_display,
            name,
            kind="agent",
            must_close=False,
            work_body=work_body,
            color_key=None,
        ) as row:
            session = self._build_preflight_session(mount_path)
            runner = ContainerRunner(
                name,
                session,
                status_display=status_display,
                cfg=self._cfg,
            )
            try:
                try:
                    await runner.setup(git_name, git_email, work_body)
                except DockerError as exc:
                    raise SetupPhaseError("preflight", str(exc)) from exc
                failures = await runner.preflight(
                    list(self._cfg.preflight_checks),
                    python_dependency_metadata=python_dependency_metadata,
                )
                if not failures:
                    row.close("finished, all tests green")
                return failures
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass
