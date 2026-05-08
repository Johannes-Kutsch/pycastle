import asyncio
import re
import shlex
from collections.abc import Callable
from pathlib import Path

from .agent_output_protocol import AgentOutput, AgentRole
from .config import Config
from .docker_session import DockerSession
from .errors import DockerError
from .prompt_pipeline import prepare_prompt
from .session_resume import RunKind
from .status_display import PlainStatusDisplay
from .stream_session import WorkStream

_DEFAULTS_PROMPTS = Path(__file__).parent / "defaults" / "prompts"
_RESUME_PROMPT_FILE = _DEFAULTS_PROMPTS / "_resume-prompt.md"


def _build_claude_command(
    model: str = "",
    effort: str = "",
    run_kind: RunKind = RunKind.FRESH,
    session_uuid: str | None = None,
) -> str:
    flags = "--verbose --dangerously-skip-permissions --output-format stream-json -p -"
    if model:
        flags += f" --model {model}"
    if effort:
        flags += f" --effort {effort}"
    if session_uuid:
        if run_kind == RunKind.RESUME:
            flags += f" --resume {session_uuid}"
        else:
            flags += f" --session-id {session_uuid}"
    return f"claude {flags} < /tmp/.pycastle_prompt"


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
    ) -> None:
        self.name = name
        self._session = session
        self.model = model
        self.effort = effort
        self._cfg = cfg
        self._status_display = (
            status_display if status_display is not None else PlainStatusDisplay()
        )
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self._log_path = self._cfg.logs_dir / f"{slug}.log"

    @property
    def log_path(self) -> Path:
        return self._log_path

    async def setup(self, git_name: str, git_email: str, work_body: str = "") -> None:
        self._cfg.logs_dir.mkdir(parents=True, exist_ok=True)
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
        self, checks: list[tuple[str, str]]
    ) -> list[tuple[str, str, str]]:
        loop = asyncio.get_running_loop()
        failures: list[tuple[str, str, str]] = []
        for check_name, command in checks:
            self._status_display.update_phase(self.name, f"Running {check_name} Checks")
            try:
                await loop.run_in_executor(None, self._session.exec_simple, command)
            except DockerError as exc:
                failures.append((check_name, command, str(exc)))
        return failures

    async def work(
        self,
        role: AgentRole,
        prompt_file: Path,
        prompt_args: dict[str, str],
        *,
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
        is_failsoft_recovery: bool = False,
        send_role_prompt_on_resume: bool = False,
    ) -> AgentOutput:
        self._status_display.update_phase(self.name, "Prepare")
        loop = asyncio.get_running_loop()

        async def container_exec(cmd: str) -> str:
            return await loop.run_in_executor(None, self._session.exec_simple, cmd)

        if run_kind == RunKind.RESUME and not send_role_prompt_on_resume:
            prompt = _RESUME_PROMPT_FILE.read_text(encoding="utf-8")
        elif run_kind == RunKind.RESUME and send_role_prompt_on_resume:
            prompt = await prepare_prompt(prompt_file, prompt_args, container_exec)
        else:
            role_prompt = await prepare_prompt(prompt_file, prompt_args, container_exec)
            if is_failsoft_recovery:
                resume_text = _RESUME_PROMPT_FILE.read_text(encoding="utf-8")
                prompt = resume_text + "\n\n" + role_prompt
            else:
                prompt = role_prompt

        self._status_display.update_phase(self.name, "Work")

        def on_turn(turn: str) -> None:
            self._status_display.print(self.name, turn)

        return await loop.run_in_executor(
            None,
            lambda: self._run_streaming(role, prompt, on_turn, run_kind, session_uuid),
        )

    def _run_streaming(
        self,
        role: AgentRole,
        prompt: str,
        on_turn: Callable[[str], None],
        run_kind: RunKind = RunKind.FRESH,
        session_uuid: str | None = None,
    ) -> AgentOutput:
        self._session.write_file(prompt, "/tmp/.pycastle_prompt")
        chunks = self._session.exec_stream(
            _build_claude_command(
                model=self.model,
                effort=self.effort,
                run_kind=run_kind,
                session_uuid=session_uuid,
            )
        )
        try:
            ws = WorkStream(
                chunks,
                self._log_path,
                self._cfg.idle_timeout,
                lambda: self._status_display.reset_idle_timer(self.name),
            )
            return ws.run(role, on_turn)
        finally:
            try:
                self._session.exec_simple("rm -f /tmp/.pycastle_prompt")
            except Exception:
                pass
