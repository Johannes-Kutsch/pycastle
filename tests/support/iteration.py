import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import AgentOutput, AgentSuccessOutput
from pycastle.agents.runner import (
    AgentRunnerProtocol,
    RunRequest,
    translate_run_outcome,
)
from pycastle.config import Config
from pycastle.display.status_display import ModelDisplayMetadata, StatusDisplay
from pycastle.errors import HardAgentError
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.iteration._deps import Deps, Logger
from pycastle.iteration.preflight import PreflightCache, PreflightReady, PreflightResult
from pycastle.services import GitService, GithubService, ServiceRegistry


class RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[dict, Exception]] = []
        self.internal_errors: list[tuple[str, Exception, Exception | None]] = []
        self.agent_outputs: list[tuple[str, str]] = []

    def log_error(self, issue: dict, error: Exception) -> None:
        self.errors.append((issue, error))

    def log_internal_error(
        self, label: str, error: Exception, cause: Exception | None = None
    ) -> None:
        self.internal_errors.append((label, error, cause))

    def log_agent_output(self, agent_name: str, output: str) -> None:
        self.agent_outputs.append((agent_name, output))


class RecordingStatusDisplay:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.register_calls: list[dict[str, Any]] = []
        self.remove_calls: list[dict[str, str]] = []
        self.phase_updates: list[tuple[str, str]] = []

    def register(
        self,
        caller: str,
        kind: str,
        startup_message: str = "started",
        work_body: str = "",
        initial_phase: str = "Setup",
        color_key: int | None = None,
        model_display: ModelDisplayMetadata | None = None,
    ) -> None:
        self.register_calls.append(
            {
                "caller": caller,
                "kind": kind,
                "startup_message": startup_message,
                "work_body": work_body,
                "initial_phase": initial_phase,
                "color_key": color_key,
                "model_display": model_display,
            }
        )
        self.calls.append(
            ("register", caller, kind, startup_message, initial_phase, model_display)
        )

    def update_phase(self, name: str, phase: str) -> None:
        self.phase_updates.append((name, phase))
        self.calls.append(("update_phase", name, phase))

    def reset_idle_timer(self, name: str) -> None:
        self.calls.append(("reset_idle_timer", name))

    def update_tokens(self, name: str, current_tokens: int) -> None:
        self.calls.append(("update_tokens", name, current_tokens))

    def remove(
        self,
        caller: str,
        shutdown_message: str = "finished",
        shutdown_style: str = "success",
    ) -> None:
        self.remove_calls.append(
            {
                "caller": caller,
                "shutdown_message": shutdown_message,
                "shutdown_style": shutdown_style,
            }
        )
        self.calls.append(("remove", caller, shutdown_message, shutdown_style))

    def print(self, caller: str, message: object, style: str | None = None) -> None:
        self.calls.append(("print", caller, message, style))


class FakeAgentRunner:
    """Queue-based test double: pop responses in order, record all calls, or delegate to side_effect."""

    def __init__(
        self,
        responses: list[AgentOutput | BaseException] | None = None,
        *,
        side_effect: Callable[..., Any] | None = None,
        preflight_responses: list[list[PreflightCommandFailure] | BaseException]
        | None = None,
    ) -> None:
        self._responses: list[AgentOutput | BaseException] = list(responses or [])
        self._side_effect = side_effect
        self._preflight_unlimited = preflight_responses is None
        self._preflight_responses: list[
            list[PreflightCommandFailure] | BaseException
        ] = list(preflight_responses or [])
        self.calls: list[RunRequest] = []
        self.preflight_calls: list[dict] = []

    async def run(self, request: RunRequest) -> AgentSuccessOutput:
        return await translate_run_outcome(self._run(request), request)

    async def _run(self, request: RunRequest) -> AgentOutput:
        self.calls.append(request)
        try:
            if self._side_effect is not None:
                result = self._side_effect(request)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, BaseException):
                    raise result
                return result
            if not self._responses:
                raise AssertionError(
                    f"FakeAgentRunner queue exhausted — unexpected call for agent {request.name!r}"
                )
            response = self._responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        except HardAgentError as err:
            err.caller = request.name
            raise

    async def run_preflight(
        self,
        *,
        name: str,
        mount_path: Path,
        stage: str = "",
        status_display: StatusDisplay | None = None,
        work_body: str = "",
    ) -> list[PreflightCommandFailure]:
        call = {
            "name": name,
            "mount_path": mount_path,
            "stage": stage,
            "status_display": status_display,
            "work_body": work_body,
        }
        self.preflight_calls.append(call)
        if not self._preflight_responses:
            if self._preflight_unlimited:
                return []
            raise AssertionError(
                f"FakeAgentRunner preflight queue exhausted — unexpected call for agent {name!r}"
            )
        response = self._preflight_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class StubPreflightCache:
    """Test double: always returns a fixed verdict from get_safe_sha()."""

    def __init__(self, verdict: PreflightResult | None = None) -> None:
        self._verdict: PreflightResult = verdict or PreflightReady(sha="abc123")

    async def get_safe_sha(self, deps: Any) -> PreflightResult:
        return self._verdict


def _default_git_service() -> GitService:
    git_svc = MagicMock(spec=GitService)
    git_svc.verify_ref_exists.return_value = False

    registered: list[Path] = []

    def _fake_list_worktrees(repo: Path) -> list[Path]:
        return list(registered)

    def _fake_create_worktree(
        repo: Path, path: Path, branch: str, sha: str | None = None
    ) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\nname='t'\n")
        registered.append(path)

    def _fake_remove_worktree(repo: Path, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
        registered[:] = [
            registered_path for registered_path in registered if registered_path != path
        ]

    git_svc.list_worktrees.side_effect = _fake_list_worktrees
    git_svc.create_worktree.side_effect = _fake_create_worktree
    git_svc.remove_worktree.side_effect = _fake_remove_worktree
    return git_svc


def _make_deps(
    repo_root: Path,
    agent_runner: AgentRunnerProtocol | Callable[..., Any],
    *,
    git_svc: GitService | None = None,
    github_svc: GithubService | None = None,
    cfg: Config | None = None,
    logger: Logger | None = None,
    status_display: StatusDisplay | None = None,
    preflight_cache: PreflightCache | StubPreflightCache | None = None,
    service_registry: ServiceRegistry | None = None,
    preflight_responses: list[list[PreflightCommandFailure] | BaseException]
    | None = None,
    setup_worktrees: bool = False,
) -> Deps:
    if hasattr(agent_runner, "run") and hasattr(agent_runner, "run_preflight"):
        runner = agent_runner
    else:
        runner = FakeAgentRunner(
            side_effect=agent_runner,
            preflight_responses=preflight_responses,
        )

    resolved_git_svc = git_svc if git_svc is not None else _default_git_service()
    resolved_github_svc = (
        github_svc if github_svc is not None else MagicMock(spec=GithubService)
    )

    if setup_worktrees:
        git_mock = cast(Any, resolved_git_svc)
        github_mock = cast(Any, resolved_github_svc)
        registered: list[Path] = []

        def _fake_list_worktrees(repo):
            return list(registered)

        def _fake_create_worktree(repo, path, branch, sha=None):
            path.mkdir(parents=True, exist_ok=True)
            (path / "pyproject.toml").write_text("[project]\nname='t'\n")
            registered.append(path)

        def _fake_remove_worktree(repo, path):
            shutil.rmtree(path, ignore_errors=True)
            registered[:] = [p for p in registered if p != path]

        git_mock.list_worktrees.side_effect = _fake_list_worktrees
        git_mock.create_worktree.side_effect = _fake_create_worktree
        git_mock.remove_worktree.side_effect = _fake_remove_worktree
        if isinstance(
            github_mock.get_all_open_issues_lightweight.return_value, MagicMock
        ):
            github_mock.get_all_open_issues_lightweight.return_value = []

    return Deps(
        repo_root=repo_root,
        git_svc=resolved_git_svc,
        github_svc=resolved_github_svc,
        agent_runner=runner,
        cfg=cfg if cfg is not None else Config(),
        logger=logger if logger is not None else RecordingLogger(),
        status_display=status_display
        if status_display is not None
        else RecordingStatusDisplay(),
        service_registry=service_registry,
        preflight_cache=preflight_cache
        if preflight_cache is not None
        else StubPreflightCache(),  # type: ignore[arg-type]
    )
