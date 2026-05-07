import dataclasses
import shutil
import traceback
from pathlib import Path
from typing import Any, Protocol

from .account_pool import AccountPool
from .agent_output_protocol import AgentOutput, AgentRole, CommitMessageParseError
from .agent_result import CancellationToken, PreflightFailure
from .config import Config
from .container_runner import ContainerRunner
from .docker_session import DockerSession, build_volume_spec
from .errors import AgentTimeoutError, UsageLimitError
from .session_resume import (
    RunKind,
    decide_agent_run_kind,
    derived_session_uuid,
    has_resumable_session,
)
from .services import GitService
from .status_display import PlainStatusDisplay

_CONTAINER_WORKSPACE = "/home/agent/workspace"


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
        account_pool: AccountPool | None = None,
    ) -> None:
        self._env = env
        self._cfg = cfg
        self._git_service = git_service
        self._docker_client = docker_client
        self._account_pool = account_pool

    def _build_session(
        self, mount_path: Path, role: AgentRole | None = None
    ) -> tuple[DockerSession, str | None]:
        volumes, auto_overlay = build_volume_spec(mount_path)
        container_env = dict(self._env)
        picked_token: str | None = None
        if self._account_pool is not None:
            _, picked_token = self._account_pool.pick()
            container_env["CLAUDE_CODE_OAUTH_TOKEN"] = picked_token
        if role is not None:
            container_env["CLAUDE_CONFIG_DIR"] = (
                f"{_CONTAINER_WORKSPACE}/.pycastle-session/{role.value}/"
            )
        return (
            DockerSession(
                volumes=volumes,
                container_env=container_env,
                image_name=self._cfg.docker_image_name,
                cfg=self._cfg,
                docker_client=self._docker_client,
                auto_overlay=auto_overlay,
            ),
            picked_token,
        )

    async def run(self, request: RunRequest) -> AgentOutput | PreflightFailure:
        from .iteration._rows import agent_row

        name = request.name
        prompt_file = request.prompt_file
        mount_path = request.mount_path
        role = request.role
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
            raise UsageLimitError(reset_time=None)

        role_session_dir = mount_path / ".pycastle-session" / role.value
        session_dir_present = has_resumable_session(role_session_dir)
        run_kind = decide_agent_run_kind(role, session_dir_present=session_dir_present)
        session_uuid = derived_session_uuid(role, mount_path)

        is_failsoft_recovery = False

        async with agent_row(status_display, name, work_body):
            session, picked_token = self._build_session(mount_path, role)
            runner = ContainerRunner(
                name,
                session,
                model=model,
                effort=effort,
                status_display=status_display,
                cfg=self._cfg,
            )
            try:
                git_name = self._git_service.get_user_name()
                git_email = self._git_service.get_user_email()
                await runner.setup(git_name, git_email, work_body)
                if not skip_preflight:
                    failures = await runner.preflight(list(self._cfg.preflight_checks))
                    if failures:
                        return PreflightFailure(failures=tuple(failures))

                if run_kind == RunKind.FRESH:
                    shutil.rmtree(role_session_dir, ignore_errors=True)
                    role_session_dir.mkdir(parents=True, exist_ok=True)

                retries_left = self._cfg.timeout_retries
                while True:
                    try:
                        return await runner.work(
                            role,
                            prompt_file,
                            prompt_args or {},
                            run_kind=run_kind,
                            session_uuid=session_uuid,
                            is_failsoft_recovery=is_failsoft_recovery,
                        )
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
                        if self._account_pool is not None and picked_token is not None:
                            self._account_pool.mark_exhausted(
                                picked_token, err.reset_time
                            )
                        _token.cancel()
                        raise
                    except CommitMessageParseError:
                        raise
                    except Exception:
                        if run_kind == RunKind.RESUME and not is_failsoft_recovery:
                            tb = traceback.format_exc()
                            self._log_failsoft(name, tb)
                            shutil.rmtree(role_session_dir, ignore_errors=True)
                            role_session_dir.mkdir(parents=True, exist_ok=True)
                            run_kind = RunKind.FRESH
                            is_failsoft_recovery = True
                        else:
                            raise
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass

    def _log_failsoft(self, name: str, tb: str) -> None:
        try:
            self._cfg.logs_dir.mkdir(parents=True, exist_ok=True)
            with open(self._cfg.logs_dir / "errors.log", "a", encoding="utf-8") as f:
                f.write(f"[ResumeFailSoft] {name}\n{tb}\n")
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
        from .iteration._rows import agent_row

        if status_display is None:
            status_display = PlainStatusDisplay()

        git_name = self._git_service.get_user_name()
        git_email = self._git_service.get_user_email()
        async with agent_row(status_display, name, work_body):
            session, _picked_token = self._build_session(mount_path)
            runner = ContainerRunner(
                name,
                session,
                status_display=status_display,
                cfg=self._cfg,
            )
            try:
                await runner.setup(git_name, git_email, work_body)
                return await runner.preflight(list(self._cfg.preflight_checks))
            finally:
                try:
                    session.__exit__(None, None, None)
                except Exception:
                    pass
