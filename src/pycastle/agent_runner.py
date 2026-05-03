import dataclasses
from pathlib import Path
from typing import Any, Protocol

from .agent_output_protocol import AgentOutput, AgentRole
from .agent_result import CancellationToken, PreflightFailure
from .config import Config
from .container_runner import ContainerRunner
from .errors import AgentTimeoutError, UsageLimitError
from .services import GitService
from .status_display import PlainStatusDisplay


@dataclasses.dataclass
class RunRequest:
    name: str
    prompt_file: Path
    mount_path: Path
    role: AgentRole = AgentRole.IMPLEMENTER
    prompt_args: dict[str, str] | None = None
    skip_preflight: bool = False
    model: str = ""
    effort: str = ""
    stage: str = ""
    token: CancellationToken | None = None
    status_display: Any = None
    issue_title: str = ""
    work_body: str = ""


class AgentRunnerProtocol(Protocol):
    async def run(self, request: RunRequest) -> AgentOutput | PreflightFailure: ...

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
    ) -> None:
        self._env = env
        self._cfg = cfg
        self._git_service = git_service
        self._docker_client = docker_client

    async def run(self, request: RunRequest) -> AgentOutput | PreflightFailure:
        name = request.name
        prompt_file = request.prompt_file
        mount_path = request.mount_path
        prompt_args = request.prompt_args
        skip_preflight = request.skip_preflight
        model = request.model
        effort = request.effort
        token = request.token
        status_display = request.status_display
        work_body = request.work_body

        if status_display is None:
            status_display = PlainStatusDisplay()

        _token = token if token is not None else CancellationToken()
        if _token.is_cancelled:
            raise UsageLimitError("Agent cancelled due to usage limit")

        runner = ContainerRunner(
            name,
            mount_path,
            self._env,
            model=model,
            effort=effort,
            docker_client=self._docker_client,
            status_display=status_display,
            cfg=self._cfg,
        )
        try:
            git_name = self._git_service.get_user_name()
            git_email = self._git_service.get_user_email()
            await runner.setup(git_name, git_email, work_body)
            await runner.prepare(prompt_file, prompt_args or {})
            if not skip_preflight:
                failures = await runner.preflight(list(self._cfg.preflight_checks))
                if failures:
                    return PreflightFailure(failures=tuple(failures))
            retries_left = self._cfg.timeout_retries
            while True:
                try:
                    output = await runner.work(request.role)
                    return output
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
                except UsageLimitError:
                    _token.cancel(preserve_worktree=True)
                    raise
        finally:
            status_display.remove(name)
            try:
                runner.__exit__(None, None, None)
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
        if status_display is None:
            status_display = PlainStatusDisplay()

        git_name = self._git_service.get_user_name()
        git_email = self._git_service.get_user_email()
        runner = ContainerRunner(
            name,
            mount_path,
            self._env,
            docker_client=self._docker_client,
            status_display=status_display,
            cfg=self._cfg,
        )
        try:
            await runner.setup(git_name, git_email, work_body)
            return await runner.preflight(list(self._cfg.preflight_checks))
        finally:
            status_display.remove(name)
            try:
                runner.__exit__(None, None, None)
            except Exception:
                pass
