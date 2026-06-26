"""Tests for AgentRunner and FakeAgentRunner."""

import asyncio
import docker
import agent_runtime
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import (
    AgentOutputProtocolError,
    AgentRole,
    CommitMessageOutput,
    CompletionOutput,
    FailedOutput,
    IssueOutput,
    PlanParseError,
    PlannerOutput,
)
from pycastle.agents._work_invocation import (
    ProtocolOutputAdapter,
    RunSessionPlan as RuntimeRunSessionPlan,
    TextOutputAdapter,
    WorkInvocationDependencies,
    WorkInvocationRequest,
    format_transient_status_message,
    invoke_work,
)
from pycastle.agents.result import CancellationToken
from pycastle.agents.runner import AgentRunner, RunRequest, _stage_key_for_role
from pycastle.runtime_session import (
    ProviderSessionPreferences,
    ProviderSessionPreferencesRequest,
    ProviderSessionState,
    ProviderSessionStateRequest,
)
from pycastle.config import Config
from pycastle.errors import (
    AgentCredentialFailureError,
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.provider_session_adapter import provider_session_adapter_for_service
from pycastle.prompts.dispatch import PromptInvocation, build_prompt_invocation
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.session.agent import RunSessionPlan
from pycastle.session import ProviderRunState, RoleSession, RunKind
from pycastle.session_planning import (
    ProviderRunStatePlanRequest,
    plan_provider_run_state,
)
from pycastle.session.role import session_uuid_for_role_session_path
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)
from pycastle.infrastructure.container_runner import ContainerRunner
from pycastle.services.agent_service import ParsedTurn, Result
from pycastle.services import CodexService, GitCommandError, GitService, OpenCodeService
from pycastle.services.claude_service import ClaudeService
from pycastle.services.flag_profiles import AgentToolPolicyGroup
from pycastle.display.status_display import ModelDisplayMetadata
from tests.support import FakeAgentRunner, RecordingStatusDisplay


def _provider_run_state_plan_request(
    *,
    worktree: Path,
    role: AgentRole,
    namespace: str,
    service: Any,
) -> ProviderRunStatePlanRequest:
    return ProviderRunStatePlanRequest(
        worktree=worktree,
        role=role,
        namespace=namespace,
        service=service,
        role_session=RoleSession(worktree, role, namespace),
        provider_session_adapter=provider_session_adapter_for_service(service),
    )


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _runtime_default_claude_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pycastle.agents.runner.ClaudeService",
        lambda: ClaudeService(accounts=[("primary", "tok-primary")]),
    )


class _PreparedRunSessionStandIn:
    def __init__(
        self,
        *,
        initial_run_kind: RunKind,
        initial_provider_session_id: str | None,
        resumable_run_kind: RunKind | None = None,
        resumable_provider_session_id: str | None = None,
        provider_state_dir_container_path: str | None = None,
    ) -> None:
        self.provider_state_dir_container_path = provider_state_dir_container_path
        self.prepare_for_run_calls = 0
        self.initial_session = _PreparedProviderRunSessionStandIn(
            run_kind=initial_run_kind,
            provider_session_id=initial_provider_session_id,
        )
        self.resumable_session = _PreparedProviderRunSessionStandIn(
            run_kind=resumable_run_kind or initial_run_kind,
            provider_session_id=(
                initial_provider_session_id
                if resumable_provider_session_id is None
                else resumable_provider_session_id
            ),
        )

    def prepare_for_run(self) -> None:
        self.prepare_for_run_calls += 1

    def initial_provider_run_session(self) -> "_PreparedProviderRunSessionStandIn":
        return self.initial_session

    def resumable_provider_run_session(self) -> "_PreparedProviderRunSessionStandIn":
        return self.resumable_session

    def protocol_reprompt_provider_run_session(
        self,
    ) -> "_PreparedProviderRunSessionStandIn | None":
        return None


def _role_session_session_uuid(role_session: object) -> str:
    role_session_path = getattr(role_session, "path", None)
    if isinstance(role_session_path, Path):
        identity_uuid = session_uuid_for_role_session_path(role_session_path)
        if identity_uuid is not None:
            return identity_uuid
    legacy = getattr(role_session, "session_uuid", None)
    if callable(legacy):
        return legacy()
    raise AssertionError("Unable to derive role session identifier")


class _PreparedRunSessionWithRepromptStandIn(_PreparedRunSessionStandIn):
    def __init__(
        self,
        *,
        initial_run_kind: RunKind,
        initial_provider_session_id: str | None,
        reprompt_run_kind: RunKind,
        reprompt_provider_session_id: str | None,
        provider_state_dir_container_path: str | None = None,
    ) -> None:
        super().__init__(
            initial_run_kind=initial_run_kind,
            initial_provider_session_id=initial_provider_session_id,
            provider_state_dir_container_path=provider_state_dir_container_path,
        )
        self.reprompt_session = _PreparedProviderRunSessionStandIn(
            run_kind=reprompt_run_kind,
            provider_session_id=reprompt_provider_session_id,
        )

    def protocol_reprompt_provider_run_session(
        self,
    ) -> "_PreparedProviderRunSessionStandIn":
        return self.reprompt_session


class _PreparedProviderRunSessionStandIn:
    def __init__(self, *, run_kind: RunKind, provider_session_id: str | None) -> None:
        self.run_kind = run_kind
        self.provider_session_id = provider_session_id
        self.recorded_provider_session_ids: list[str] = []
        self.successful_run_calls = 0

    def record_provider_session_id(self, provider_session_id: str) -> None:
        self.provider_session_id = provider_session_id
        self.recorded_provider_session_ids.append(provider_session_id)

    def record_successful_run(self) -> None:
        self.successful_run_calls += 1


def _make_cfg(tmp_path: Path, **kwargs) -> Config:
    """Create a Config with minimal project-local prompt overrides for AgentRunner tests."""
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "coordination").mkdir(exist_ok=True)
    (prompts_dir / "shared").mkdir(exist_ok=True)
    (prompts_dir / "coordination/plan.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "shared/resume.md").write_text("resume", encoding="utf-8")
    return Config(logs_dir=tmp_path, **kwargs)


def _managed_mount(repo_root: Path, name: str = "issue-1") -> Path:
    if not repo_root.exists():
        return repo_root
    if (
        repo_root.parent.name == ".worktrees"
        and repo_root.parent.parent.name == "pycastle"
    ):
        repo_root.mkdir(parents=True, exist_ok=True)
        return repo_root
    mount_path = repo_root / "pycastle" / ".worktrees" / name
    mount_path.mkdir(parents=True, exist_ok=True)
    return mount_path


def _run_request(*, service: str = "claude", **kwargs) -> RunRequest:
    template = kwargs.pop("template")
    scope_args = kwargs.pop(
        "scope_args",
        {placeholder: "" for placeholder in template.scope.placeholders},
    )
    send_role_prompt_on_resume = kwargs.pop("send_role_prompt_on_resume", False)
    mount_path = kwargs.get("mount_path")
    if isinstance(mount_path, Path):
        kwargs["mount_path"] = _managed_mount(mount_path)
    return RunRequest(
        service=service,
        prompt=build_prompt_invocation(
            template,
            scope_args,
            send_role_prompt_on_resume=send_role_prompt_on_resume,
        ),
        **kwargs,
    )


_PLAN_TEMPLATE = PromptTemplate.PLAN
_PLAN_SCOPE_ARGS = {"ALL_OPEN_ISSUES_JSON": "[]", "READY_FOR_AGENT_ISSUES_JSON": "[]"}

# A minimal NDJSON stream that process_stream accepts as CommitMessageOutput (IMPLEMENTER role)
_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<commit_message>done</commit_message>", "is_error": false}\n'
]

# A minimal NDJSON stream that process_stream accepts as CommitMessageOutput (REVIEWER role)
_REVIEWER_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<commit_message>done</commit_message>", "is_error": false}\n'
]

# A minimal NDJSON stream that process_stream accepts as CommitMessageOutput (MERGER role)
_MERGER_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<commit_message>done</commit_message>", "is_error": false}\n'
]

_DIVERGENCE_RESOLVER_FAILED_STREAM = [
    b'{"type": "result", "result": "<promise>FAILED</promise>", "is_error": false}\n'
]

_CODEX_COMPLETE_STREAM = [
    b'{"type":"thread.started","thread_id":"thread-from-fresh"}\n',
    b'{"type":"item.completed","item":{"type":"agent_message",'
    b'"content":"<commit_message>done</commit_message>"}}\n',
]


class _PlanRecordingClaudeService(ClaudeService):
    def __init__(self) -> None:
        super().__init__()
        self.fail_provider_session_state = False
        self.build_env_state_dir_args: list[str | None] = []

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        if self.fail_provider_session_state:
            raise AssertionError("provider_session_state should not be recomputed")
        return super().provider_session_state(request)

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        self.build_env_state_dir_args.append(state_dir_container_path)
        return super().build_env(
            state_dir_container_path=state_dir_container_path,
            token=token,
        )


# ── FakeAgentRunner: queue behaviour ─────────────────────────────────────────


def test_fake_agent_runner_returns_queued_completion_output():
    fake = FakeAgentRunner([CompletionOutput()])
    result = asyncio.run(
        fake.run(
            _run_request(
                name="Tester",
                template=_PLAN_TEMPLATE,
                mount_path=Path("/workspace"),
            )
        )
    )
    assert isinstance(result, CompletionOutput)


def test_fake_agent_runner_raises_queued_exception():
    fake = FakeAgentRunner([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="Tester",
                    template=_PLAN_TEMPLATE,
                    mount_path=Path("/workspace"),
                )
            )
        )


def test_fake_agent_runner_raises_assertion_error_when_queue_exhausted():
    fake = FakeAgentRunner([])
    with pytest.raises(AssertionError, match="queue exhausted"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="Unexpected",
                    template=_PLAN_TEMPLATE,
                    mount_path=Path("/workspace"),
                )
            )
        )


def test_fake_agent_runner_exhaustion_error_includes_agent_name():
    fake = FakeAgentRunner([])
    with pytest.raises(AssertionError, match="MyAgent"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="MyAgent",
                    template=_PLAN_TEMPLATE,
                    mount_path=Path("/workspace"),
                )
            )
        )


def test_fake_agent_runner_pops_responses_in_order():
    r1, r2, r3 = CompletionOutput(), CompletionOutput(), CompletionOutput()
    fake = FakeAgentRunner([r1, r2, r3])
    run = fake.run

    async def _collect():
        m = Path("/w")
        return [
            await run(_run_request(name="A", template=_PLAN_TEMPLATE, mount_path=m)),
            await run(_run_request(name="B", template=_PLAN_TEMPLATE, mount_path=m)),
            await run(_run_request(name="C", template=_PLAN_TEMPLATE, mount_path=m)),
        ]

    results = asyncio.run(_collect())
    assert results == [r1, r2, r3]


def test_fake_agent_runner_records_all_calls():
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    mount = Path("/workspace")

    asyncio.run(
        fake.run(_run_request(name="X", template=_PLAN_TEMPLATE, mount_path=mount))
    )
    asyncio.run(
        fake.run(_run_request(name="Y", template=_PLAN_TEMPLATE, mount_path=mount))
    )

    assert len(fake.calls) == 2
    assert fake.calls[0].name == "X"
    assert fake.calls[1].name == "Y"


def test_fake_agent_runner_records_call_kwargs():
    fake = FakeAgentRunner([CompletionOutput()])
    mount = Path("/workspace")

    asyncio.run(
        fake.run(
            _run_request(
                name="Planner",
                template=PromptTemplate.PLAN,
                mount_path=mount,
                scope_args={
                    "ALL_OPEN_ISSUES_JSON": "[]",
                    "READY_FOR_AGENT_ISSUES_JSON": "[]",
                },
                model="claude-3",
                effort="high",
                stage="plan",
            )
        )
    )

    call = fake.calls[0]
    assert call.name == "Planner"
    assert call.prompt.template == PromptTemplate.PLAN
    assert call.mount_path == mount
    assert call.prompt.scope_args == {
        "ALL_OPEN_ISSUES_JSON": "[]",
        "READY_FOR_AGENT_ISSUES_JSON": "[]",
    }
    assert call.model == "claude-3"
    assert call.effort == "high"
    assert call.stage == "plan"


def test_fake_agent_runner_starts_with_empty_calls():
    fake = FakeAgentRunner([CompletionOutput()])
    assert fake.calls == []


# ── FakeAgentRunner: side_effect mode ────────────────────────────────────────


def test_fake_agent_runner_side_effect_is_called_with_run_request():
    received: dict = {}
    completion = CompletionOutput()

    async def _effect(request: RunRequest):
        received["name"] = request.name
        return completion

    fake = FakeAgentRunner(side_effect=_effect)
    result = asyncio.run(
        fake.run(
            _run_request(
                name="SideEffectAgent", template=_PLAN_TEMPLATE, mount_path=Path("/w")
            )
        )
    )

    assert result is completion
    assert received["name"] == "SideEffectAgent"


def test_fake_agent_runner_side_effect_can_raise():
    async def _effect(request: RunRequest):
        raise ValueError("side effect error")

    fake = FakeAgentRunner(side_effect=_effect)
    with pytest.raises(ValueError, match="side effect error"):
        asyncio.run(
            fake.run(
                _run_request(
                    name="Agent", template=_PLAN_TEMPLATE, mount_path=Path("/w")
                )
            )
        )


def test_fake_agent_runner_side_effect_still_records_calls():
    async def _effect(request: RunRequest):
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_effect)
    asyncio.run(
        fake.run(
            _run_request(
                name="Recorded", template=_PLAN_TEMPLATE, mount_path=Path("/w")
            )
        )
    )

    assert len(fake.calls) == 1
    assert fake.calls[0].name == "Recorded"


def test_fake_agent_runner_side_effect_can_be_synchronous():
    completion = CompletionOutput()

    def _sync_effect(request: RunRequest):
        return completion

    fake = FakeAgentRunner(side_effect=_sync_effect)
    result = asyncio.run(
        fake.run(
            _run_request(name="Agent", template=_PLAN_TEMPLATE, mount_path=Path("/w"))
        )
    )

    assert result is completion


# ── AgentRunner: helpers ──────────────────────────────────────────────────────


def _make_docker_client(chunks: list[bytes]) -> MagicMock:
    """Mock docker client whose streaming exec_run replays the given byte chunks."""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            result = MagicMock()
            result.output = iter(chunks)
            return result
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def _make_docker_client_with_setup_failure(message: str) -> MagicMock:
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            result = MagicMock()
            result.output = iter(_COMPLETE_STREAM)
            return result
        command = " ".join(args[0]) if args else ""
        if "pip install" in command:
            return MagicMock(exit_code=1, output=(b"", message.encode("utf-8")))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def _make_docker_client_with_start_failure(message: str) -> MagicMock:
    mock_client = MagicMock()
    mock_client.containers.run.side_effect = docker.errors.APIError(message)
    return mock_client


def _make_docker_client_with_work_failure(message: str) -> MagicMock:
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            raise DockerError(message)
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def _make_git_service() -> MagicMock:
    svc = MagicMock(spec=GitService)
    svc.get_user_name.return_value = "Alice"
    svc.get_user_email.return_value = "alice@example.com"
    svc.is_working_tree_clean.return_value = True
    return svc


class _CompletedRuntimeClient:
    async def run_new_session(self, request):
        del request
        return agent_runtime.RuntimeOutcome(
            kind=agent_runtime.runtime.Completed(),
            result=agent_runtime.runtime.RunResult(
                output="<commit_message>done</commit_message>",
                usage=None,
                continuation=None,
                selected=agent_runtime.types.ResolvedProvider(
                    service="claude",
                    model="haiku",
                    effort="medium",
                ),
            ),
        )

    async def run_resumed_session(self, request):
        return await self.run_new_session(request)


class _RuntimeSequenceClient:
    def __init__(self, steps: list[object]) -> None:
        self._steps = list(steps)
        self.new_session_requests: list[object] = []
        self.resumed_session_requests: list[object] = []

    def _next(self) -> object:
        if not self._steps:
            raise AssertionError("runtime client sequence exhausted")
        step = self._steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    async def run_new_session(self, request):
        self.new_session_requests.append(request)
        return self._next()

    async def run_resumed_session(self, request):
        self.resumed_session_requests.append(request)
        return self._next()


class _FakeRuntimeSession:
    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses = responses or {}

    def __enter__(self) -> "_FakeRuntimeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def exec_simple(self, command: str, timeout: float | None = None) -> str:
        del timeout
        for needle, response in self._responses.items():
            if needle in command:
                return response
        return ""


def _completed_outcome(
    *,
    output: str,
    service: str = "claude",
    model: str = "haiku",
    effort: str = "medium",
    continuation: agent_runtime.Continuation | None = None,
) -> agent_runtime.RuntimeOutcome:
    return agent_runtime.RuntimeOutcome(
        kind=agent_runtime.runtime.Completed(),
        result=agent_runtime.runtime.RunResult(
            output=output,
            usage=None,
            continuation=continuation,
            selected=agent_runtime.types.ResolvedProvider(
                service=service,
                model=model,
                effort=effort,
            ),
        ),
    )


def _patch_runtime_runner(
    monkeypatch: pytest.MonkeyPatch,
    runner: AgentRunner,
    runtime_client: object,
    *,
    exec_responses: dict[str, str] | None = None,
) -> None:
    monkeypatch.setattr(
        ContainerRunner,
        "_get_runtime_client",
        lambda self: runtime_client,
    )
    monkeypatch.setattr(
        ContainerRunner,
        "setup",
        lambda self, git_name, git_email, work_body="": asyncio.sleep(0),
    )
    monkeypatch.setattr(ContainerRunner, "provider_argv_transform", lambda self: None)
    monkeypatch.setattr(
        runner,
        "_build_session",
        lambda mount_path, service, state_dir_container_path=None: _FakeRuntimeSession(
            exec_responses
        ),
    )


def _never_yields():
    """Generator that blocks forever without yielding — simulates a hung agent stream."""
    e = threading.Event()
    e.wait()
    yield  # make this a generator


class _RecordingAgentService:
    def __init__(
        self,
        name: str,
        *,
        state_dir_relpath: str | None = None,
        provider_run_state: ProviderRunState | None = None,
    ) -> None:
        self.name = name
        self._state_dir_relpath = state_dir_relpath
        self._provider_run_state = provider_run_state or ProviderRunState(
            RunKind.FRESH, None
        )
        self.commands: list[str] = []
        self.env_state_dirs: list[str | None] = []

    def build_command(
        self,
        role: AgentRole,
        model: str,
        effort: str,
        run_kind: RunKind,
        session_uuid: str | None,
        *,
        tool_policy=None,
    ) -> str:
        del role, model, effort, run_kind, session_uuid, tool_policy
        command = f"{self.name} exec"
        self.commands.append(command)
        return command

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        self.env_state_dirs.append(state_dir_container_path)
        env = {"PYCASTLE_TEST_SERVICE": self.name}
        if self.name == "claude":
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token or "tok-primary"
        if self.name == "opencode":
            env["OPENCODE_GO_API_KEY"] = token or "opencode-key"
        return env

    def run(
        self,
        lines: Iterable[str],
        on_provider_session_id: Callable[[str], None] | None = None,
    ) -> Iterator[ParsedTurn]:
        list(lines)
        yield Result("<commit_message>done</commit_message>")

    def is_available(self, now: datetime | None = None) -> bool:
        return True

    def next_wake_time(self) -> datetime:
        return datetime.max

    def mark_exhausted(self, reset_time: datetime | None) -> None:
        pass

    def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
        del role, namespace
        return self._state_dir_relpath

    def is_resumable(self, state_dir: Path) -> bool:
        return False

    def provider_session_preferences(
        self,
        request: ProviderSessionPreferencesRequest,
    ) -> ProviderSessionPreferences:
        del request
        return ProviderSessionPreferences()

    def provider_session_state(
        self,
        request: ProviderSessionStateRequest,
    ) -> ProviderSessionState:
        del request
        return ProviderSessionState(
            self._provider_run_state.run_kind,
            self._provider_run_state.provider_session_id,
            persist_provider_session_id=self._provider_run_state.persist_provider_session_id,
        )

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high"})

    def valid_models(self) -> frozenset[str]:
        if self.name == "codex":
            return frozenset({"gpt-5.4"})
        if self.name == "opencode":
            return frozenset({"deepseek-v4-flash"})
        return frozenset({"haiku"})


# ── AgentRunner: run() return values ─────────────────────────────────────────


def test_agent_runner_run_returns_agent_output(tmp_path):
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


def test_agent_runner_run_logs_container_launch_and_teardown_order(
    tmp_path, monkeypatch
):
    log_path = tmp_path / "worktree-lifecycle-debug.log"
    monkeypatch.setenv(
        "PYCASTLE_WORKTREE_LIFECYCLE_DEBUG_LOG",
        str(log_path),
    )
    service = _RecordingAgentService("claude")
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"claude": service},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
            )
        )
    )

    entries = [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]
    events = [entry["event"] for entry in entries]
    assert "container_launch" in events
    assert "container_teardown" in events

    launch_index = events.index("container_launch")
    teardown_index = events.index("container_teardown")
    assert launch_index < teardown_index

    launch = entries[launch_index]
    teardown = entries[teardown_index]
    assert launch["exists"] is True
    assert launch["node_type"] == "directory"
    assert launch["path"] == teardown["path"]
    assert isinstance(launch.get("uid"), int)
    assert isinstance(launch.get("gid"), int)

    assert set(service.env_state_dirs) == {
        str(_managed_mount(tmp_path) / ".pycastle-session" / "implementer")
    }


def test_agent_runner_dispatches_with_explicit_claude_service(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_service = _RecordingAgentService("codex")
    claude_service = _RecordingAgentService("claude")
    monkeypatch.setattr(
        ContainerRunner,
        "_get_runtime_client",
        lambda self: _CompletedRuntimeClient(),
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": claude_service, "codex": codex_service},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert claude_service.env_state_dirs
    assert set(claude_service.env_state_dirs) == {
        str(_managed_mount(tmp_path) / ".pycastle-session" / "implementer")
    }
    assert codex_service.env_state_dirs == []


def test_agent_runner_uses_requested_service_from_registry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    claude_service = _RecordingAgentService("claude")
    requested_service = _RecordingAgentService("codex")
    monkeypatch.setattr(
        ContainerRunner,
        "_get_runtime_client",
        lambda self: _CompletedRuntimeClient(),
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": claude_service, "codex": requested_service},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                service="codex",
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert requested_service.env_state_dirs
    assert set(requested_service.env_state_dirs) == {
        str(_managed_mount(tmp_path) / ".pycastle-session" / "implementer")
    }
    assert claude_service.env_state_dirs == []


def test_agent_runner_does_not_fall_back_to_claude_for_unknown_requested_service(
    tmp_path,
):
    claude_service = _RecordingAgentService("claude")
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": claude_service},
    )

    with pytest.raises(ValueError, match="Unknown agent service 'codex'"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    service="codex",
                )
            )
        )

    assert claude_service.env_state_dirs == []


@pytest.mark.parametrize("service_name", ["claude", "codex"])
def test_agent_runner_uses_universal_image_for_requested_service(
    tmp_path, service_name, monkeypatch: pytest.MonkeyPatch
):
    requested_service = _RecordingAgentService(service_name)
    docker_client = _make_docker_client([])
    monkeypatch.setattr(
        ContainerRunner,
        "_get_runtime_client",
        lambda self: _CompletedRuntimeClient(),
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=docker_client,
        service_registry={service_name: requested_service},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                service=service_name,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    docker_client.containers.run.assert_called_once()
    assert docker_client.containers.run.call_args.args[0] == "pycastle-test"


def test_agent_runner_requires_explicit_resolved_service_for_dispatch(tmp_path):
    cfg = _make_cfg(tmp_path, docker_image_name="pycastle-test")
    object.__setattr__(cfg, "default_service", "codex")
    codex_service = _RecordingAgentService("codex")
    claude_service = _RecordingAgentService("claude")
    docker_client = _make_docker_client([])
    runner = AgentRunner(
        {},
        cfg,
        _make_git_service(),
        docker_client=docker_client,
        service_registry={"claude": claude_service, "codex": codex_service},
    )

    with pytest.raises(ValueError, match="resolved service"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    service="",
                )
            )
        )

    assert codex_service.env_state_dirs == []
    assert claude_service.env_state_dirs == []
    docker_client.containers.run.assert_not_called()


def test_agent_runner_requires_explicit_resolved_service_for_whitespace_only_service(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client([]),
        service_registry={"claude": _RecordingAgentService("claude")},
    )

    with pytest.raises(ValueError, match="resolved service"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    service="   ",
                )
            )
        )


def test_agent_runner_fails_when_no_explicit_service_even_if_default_service_is_empty(
    tmp_path,
):
    cfg = _make_cfg(tmp_path, docker_image_name="pycastle-test")
    object.__setattr__(cfg, "default_service", "")
    docker_client = _make_docker_client([])
    runner = AgentRunner(
        {},
        cfg,
        _make_git_service(),
        docker_client=docker_client,
        service_registry={"claude": _RecordingAgentService("claude")},
    )

    with pytest.raises(ValueError, match="resolved service"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    service="",
                )
            )
        )

    docker_client.containers.run.assert_not_called()


# ── AgentRunner: error propagation ───────────────────────────────────────────


def test_agent_runner_run_raises_usage_limit_error_when_token_pre_cancelled(tmp_path):
    token = CancellationToken()
    token.cancel()
    mock_client = _make_docker_client([b"output\n"])
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    token=token,
                )
            )
        )

    mock_client.containers.run.assert_not_called()


def test_agent_runner_run_cancels_token_and_raises_on_usage_limit_in_stream(tmp_path):
    token = CancellationToken()
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.RuntimeOutcome(
                kind=agent_runtime.runtime.UsageLimited(reset_time=None),
                result=agent_runtime.runtime.RunResult(
                    output="",
                    usage=None,
                    continuation=None,
                    selected=agent_runtime.types.ResolvedProvider(
                        service="claude",
                        model="haiku",
                        effort="medium",
                    ),
                ),
            )
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        with pytest.raises(UsageLimitError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=_managed_mount(tmp_path),
                        token=token,
                    )
                )
            )

    assert token.is_cancelled


def test_agent_runner_run_raises_agent_timeout_error_when_retries_exhausted(tmp_path):
    class _TimedOutRuntimeClient:
        async def run_new_session(self, request):
            del request
            return agent_runtime.RuntimeOutcome(
                kind=agent_runtime.runtime.TimedOut(),
                result=agent_runtime.runtime.RunResult(
                    output="",
                    usage=None,
                    continuation=None,
                    selected=agent_runtime.types.ResolvedProvider(
                        service="claude",
                        model="haiku",
                        effort="medium",
                    ),
                ),
            )

        async def run_resumed_session(self, request):
            return await self.run_new_session(request)

    cfg = _make_cfg(tmp_path, idle_timeout=0.01, timeout_retries=0)
    runner = AgentRunner(
        {},
        cfg,
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
    )
    runtime_client = _TimedOutRuntimeClient()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            ContainerRunner,
            "_get_runtime_client",
            lambda self: runtime_client,
        )

        with pytest.raises(AgentTimeoutError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=_managed_mount(tmp_path),
                    )
                )
            )


def test_agent_runner_run_raises_agent_failed_error_for_non_typed_crash(tmp_path):
    from unittest.mock import AsyncMock, patch

    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    request = _run_request(
        name="Test",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=_managed_mount(tmp_path),
        role=AgentRole.IMPLEMENTER,
        session_namespace="test-ns",
    )

    with patch.object(
        runner,
        "_run",
        new=AsyncMock(return_value=FailedOutput(failure_class="non_typed_crash")),
    ):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(runner.run(request))

    err = exc_info.value
    assert err.failure_class == "non_typed_crash"
    assert err.role_value == AgentRole.IMPLEMENTER.value
    assert err.worktree_path == _managed_mount(tmp_path)
    assert err.namespace == "test-ns"


def test_agent_runner_run_raises_agent_failed_error_for_protocol_error(tmp_path):
    from unittest.mock import AsyncMock, patch

    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    request = _run_request(
        name="Test",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=_managed_mount(tmp_path),
        role=AgentRole.PLANNER,
        session_namespace="",
    )

    with patch.object(
        runner,
        "_run",
        new=AsyncMock(return_value=FailedOutput(failure_class="protocol_error")),
    ):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(runner.run(request))

    err = exc_info.value
    assert err.failure_class == "protocol_error"
    assert err.role_value == AgentRole.PLANNER.value
    assert err.worktree_path == _managed_mount(tmp_path)
    assert err.namespace == ""
    assert err.service_name == "claude"
    assert str(err.session_store) == ".pycastle-session/planner/claude"


def test_agent_runner_failed_output_reports_selected_service_session_dir(tmp_path):
    from unittest.mock import AsyncMock, patch

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        service_registry={"opencode": OpenCodeService()},
    )
    request = _run_request(
        name="Test",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=_managed_mount(tmp_path),
        role=AgentRole.PLANNER,
        service="opencode",
    )

    with patch.object(
        runner,
        "_run",
        new=AsyncMock(return_value=FailedOutput(failure_class="protocol_error")),
    ):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(runner.run(request))

    err = exc_info.value
    assert err.service_name == "opencode"
    assert str(err.session_store) == ".pycastle-session/planner/opencode"


@pytest.mark.parametrize(
    ("role", "template", "scope_args"),
    [
        (AgentRole.PLANNER, _PLAN_TEMPLATE, _PLAN_SCOPE_ARGS),
        (
            AgentRole.PREFLIGHT_ISSUE,
            PromptTemplate.PREFLIGHT_ISSUE,
            {"CHECK_NAME": "ruff", "COMMAND": "ruff check .", "OUTPUT": "missing"},
        ),
    ],
)
def test_agent_runner_run_raises_setup_phase_error_when_setup_fails_before_work(
    tmp_path,
    role,
    template,
    scope_args,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client_with_setup_failure("pip install failed"),
    )
    request = _run_request(
        name="Role Agent",
        template=template,
        scope_args=scope_args,
        mount_path=_managed_mount(tmp_path),
        role=role,
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(runner.run(request))

    assert exc_info.value.phase == role.value
    assert "pip install failed" in str(exc_info.value)


def test_agent_runner_run_raises_setup_phase_error_when_container_start_fails(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=_make_docker_client_with_start_failure(
            "exec: sleep: executable file not found in $PATH"
        ),
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Host-Check Reporter",
                    template=PromptTemplate.PREFLIGHT_ISSUE,
                    scope_args={
                        "CHECK_NAME": "[PREFLIGHT] reporter",
                        "COMMAND": "pytest",
                        "OUTPUT": "docker start failed",
                    },
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PREFLIGHT_ISSUE,
                )
            )
        )

    assert exc_info.value.phase == AgentRole.PREFLIGHT_ISSUE.value
    assert "pycastle-test" in str(exc_info.value)
    assert "sleep: executable file not found in $PATH" in str(exc_info.value)


def test_agent_runner_run_propagates_work_failures_after_setup_starts(tmp_path):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client_with_work_failure("stream broke"),
    )

    with pytest.raises(DockerError, match="stream broke"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Plan Agent",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                )
            )
        )


def test_agent_runner_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner({}, _make_cfg(tmp_path), mock_git, docker_client=mock_client)

    with pytest.raises(GitCommandError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                )
            )
        )


# ── AgentRunner: run_preflight ────────────────────────────────────────────────


def _make_preflight_docker_client(exit_code: int = 0, stdout: bytes = b"") -> MagicMock:
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        return MagicMock(exit_code=exit_code, output=(stdout, b""))

    mock_container.exec_run.side_effect = _exec_run
    return mock_client


def test_agent_runner_run_preflight_returns_empty_list_when_no_checks_configured(
    tmp_path,
):
    mock_client = _make_preflight_docker_client()
    cfg = _make_cfg(tmp_path, preflight_checks=())
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == []


def test_agent_runner_run_preflight_does_not_require_resolved_service(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    object.__setattr__(cfg, "default_service", "claude")
    runner = AgentRunner(
        {},
        cfg,
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"codex": CodexService()},
    )

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == []


def test_agent_runner_run_preflight_returns_empty_list_when_all_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == []


def test_agent_runner_run_preflight_raises_setup_phase_error_when_setup_fails(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client_with_setup_failure("pip install failed"),
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(
            runner.run_preflight(
                name="plan-sandbox", mount_path=_managed_mount(tmp_path)
            )
        )

    assert exc_info.value.phase == "preflight"
    assert "pip install failed" in str(exc_info.value)


def test_agent_runner_run_preflight_raises_setup_phase_error_when_container_start_fails(
    tmp_path,
):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=_make_docker_client_with_start_failure(
            "exec: sleep: executable file not found in $PATH"
        ),
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(
            runner.run_preflight(
                name="plan-sandbox", mount_path=_managed_mount(tmp_path)
            )
        )

    assert exc_info.value.phase == "preflight"
    assert "pycastle-test" in str(exc_info.value)
    assert "sleep: executable file not found in $PATH" in str(exc_info.value)


def test_agent_runner_run_preflight_returns_typed_failure_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(
        exit_code=1, stdout=b"E501 line too long"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert len(result) == 1
    assert result[0].check_name == "ruff"
    assert result[0].command == "ruff check ."
    assert "E501" in result[0].output


def test_agent_runner_run_preflight_collects_all_failures_when_multiple_checks_fail(
    tmp_path,
):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"check failed")
    cfg = Config(
        logs_dir=tmp_path,
        preflight_checks=(("ruff", "ruff check ."), ("mypy", "mypy .")),
    )
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert len(result) == 2
    assert result[0].check_name == "ruff"
    assert result[1].check_name == "mypy"


def test_agent_runner_run_preflight_stops_container_after_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client()
    cfg = _make_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    mock_client.containers.run.return_value.stop.assert_called()


def test_agent_runner_run_preflight_stops_container_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"check failed")
    cfg = _make_cfg(tmp_path, preflight_checks=(("lint", "lint ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    mock_client.containers.run.return_value.stop.assert_called()


def test_agent_runner_run_preflight_raises_setup_phase_error_when_pip_install_fails(
    tmp_path,
):
    # If pip install fails during Setup, the preflight container must abort via
    # the shared setup-failure path rather than continuing with ruff absent.
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pip install" in command_str:
            return MagicMock(
                exit_code=1, output=(b"", b"ERROR: Could not find a version")
            )
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(
            runner.run_preflight(
                name="plan-sandbox", mount_path=_managed_mount(tmp_path)
            )
        )

    assert exc_info.value.phase == "preflight"


def test_agent_runner_run_preflight_returns_failure_tuple_for_missing_pyproject_declared_tool(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: ruff: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        )
    ]


def test_agent_runner_run_preflight_returns_failure_tuple_for_missing_requirements_declared_tool(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['click>=8']\n", encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("ruff==0.6.9\n", encoding="utf-8")
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: ruff: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        )
    ]


def test_agent_runner_run_preflight_ignores_malformed_pyproject_and_returns_raw_failure(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text("[project\nname = 't'\n", encoding="utf-8")
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: ruff: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        )
    ]


def test_agent_runner_run_preflight_returns_failure_tuple_for_missing_undeclared_tool(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: black: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("black", "black --check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == [
        PreflightCommandFailure(
            check_name="black",
            command="black --check .",
            output="Command failed (exit 127): bash: black: command not found",
        )
    ]


def test_agent_runner_run_preflight_keeps_missing_tool_without_python_declaration_as_ordinary_failure(
    tmp_path,
):
    (tmp_path / "requirements.txt").write_text("pytest==9.0.0\n", encoding="utf-8")
    mock_client = _make_preflight_docker_client(
        exit_code=127, stdout=b"bash: shellcheck: command not found"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("shellcheck", "shellcheck ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == [
        PreflightCommandFailure(
            check_name="shellcheck",
            command="shellcheck .",
            output="Command failed (exit 127): bash: shellcheck: command not found",
        )
    ]


def test_agent_runner_run_preflight_returns_failure_tuple_for_declared_tool_project_failure(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    mock_client = _make_preflight_docker_client(
        exit_code=1, stdout=b"src/app.py:1:1: F401 imported but unused"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 1): src/app.py:1:1: F401 imported but unused",
        )
    ]


def test_agent_runner_run_preflight_returns_all_failures_after_running_later_checks(
    tmp_path,
):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 't'\ndependencies = ['ruff>=0.5']\n", encoding="utf-8"
    )
    exec_calls: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        exec_calls.append(command_str)
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "ruff check ." in command_str:
            return MagicMock(
                exit_code=127, output=(b"bash: ruff: command not found", b"")
            )
        if "mypy ." in command_str:
            return MagicMock(exit_code=1, output=(b"src/app.py:1: error: boom", b""))
        raise AssertionError(f"unexpected command: {command_str}")

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(
        tmp_path,
        preflight_checks=(("ruff", "ruff check ."), ("mypy", "mypy .")),
    )
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    ruff_idx = next(i for i, call in enumerate(exec_calls) if "ruff check ." in call)
    mypy_idx = next(i for i, call in enumerate(exec_calls) if "mypy ." in call)
    assert ruff_idx < mypy_idx
    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 127): bash: ruff: command not found",
        ),
        PreflightCommandFailure(
            check_name="mypy",
            command="mypy .",
            output="Command failed (exit 1): src/app.py:1: error: boom",
        ),
    ]


def test_agent_runner_run_preflight_returns_all_ordinary_failures_in_configured_order(
    tmp_path,
):
    exec_calls: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        exec_calls.append(command_str)
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "ruff check ." in command_str:
            return MagicMock(exit_code=1, output=(b"src/app.py:1:1: F401", b""))
        if "mypy ." in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pytest" in command_str:
            return MagicMock(exit_code=1, output=(b"FAILED tests/test_app.py", b""))
        raise AssertionError(f"unexpected command: {command_str}")

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(
        tmp_path,
        preflight_checks=(
            ("ruff", "ruff check ."),
            ("mypy", "mypy ."),
            ("pytest", "pytest"),
        ),
    )
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="Command failed (exit 1): src/app.py:1:1: F401",
        ),
        PreflightCommandFailure(
            check_name="pytest",
            command="pytest",
            output="Command failed (exit 1): FAILED tests/test_app.py",
        ),
    ]
    assert any("mypy ." in call for call in exec_calls)


def test_agent_runner_run_preflight_passes_checks_that_require_installed_tools(
    tmp_path,
):
    # Simulates the original bug: ruff fails with exit 127 (command not found)
    # if the Setup phase hasn't run pip install first. Verifies the fix: setup
    # runs before preflight so the check succeeds.
    setup_done = {"value": False}
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pip install" in command_str:
            setup_done["value"] = True
            return MagicMock(exit_code=0, output=(b"", b""))
        if "ruff check" in command_str:
            if not setup_done["value"]:
                return MagicMock(
                    exit_code=127, output=(b"bash: ruff: command not found", b"")
                )
            return MagicMock(exit_code=0, output=(b"", b""))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == []


def test_agent_runner_run_preflight_preserves_agent_user_console_script_path(
    tmp_path,
):
    setup_installed_console_script = {"value": False}
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        if "pip install" in command_str:
            setup_installed_console_script["value"] = True
            return MagicMock(exit_code=0, output=(b"", b""))
        if "demo-tool --version" in command_str:
            if not setup_installed_console_script["value"]:
                return MagicMock(
                    exit_code=127, output=(b"bash: demo-tool: command not found", b"")
                )
            if 'export PATH="/home/agent/.local/bin:$PATH";' not in command_str:
                return MagicMock(
                    exit_code=127, output=(b"bash: demo-tool: command not found", b"")
                )
            return MagicMock(exit_code=0, output=(b"demo-tool 1.0.0", b""))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("demo-tool", "demo-tool --version"),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run_preflight(name="plan-sandbox", mount_path=_managed_mount(tmp_path))
    )

    assert result == []


# ── AgentRunner: run_preflight status_display ────────────────────────────────


def test_agent_runner_run_preflight_registers_and_removes_status_row_on_success(
    tmp_path,
):
    mock_client = _make_preflight_docker_client()
    cfg = _make_cfg(tmp_path, preflight_checks=())
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks",
            mount_path=_managed_mount(tmp_path),
            status_display=display,
        )
    )

    assert (
        "register",
        "preflight-checks",
        "agent",
        "started",
        "Setup",
        None,
    ) in display.calls
    assert (
        "remove",
        "preflight-checks",
        "finished, all tests green",
        "success",
    ) in display.calls


def test_agent_runner_run_preflight_updates_phase_for_each_check(tmp_path):
    mock_client = _make_preflight_docker_client()
    checks = (("ruff", "ruff check ."), ("mypy", "mypy ."), ("pytest", "pytest"))
    cfg = _make_cfg(tmp_path, preflight_checks=checks)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks",
            mount_path=_managed_mount(tmp_path),
            status_display=display,
        )
    )

    phase_updates = [c for c in display.calls if c[0] == "update_phase"]
    assert any(c[2] == "Running ruff (1/3)" for c in phase_updates)
    assert any(c[2] == "Running mypy (2/3)" for c in phase_updates)
    assert any(c[2] == "Running pytest (3/3)" for c in phase_updates)


def test_agent_runner_run_preflight_removes_status_row_when_checks_fail(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"E501")
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks",
            mount_path=_managed_mount(tmp_path),
            status_display=display,
        )
    )

    assert ("remove", "preflight-checks", "finished", "success") in display.calls


def test_agent_runner_run_preflight_removes_status_row_when_exception_propagates(
    tmp_path,
):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _exec_run(cmd, **kwargs):
        command_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "git config" in command_str or "pip install" in command_str:
            return MagicMock(exit_code=0, output=(b"", b""))
        raise RuntimeError("unexpected container error")

    mock_container.exec_run.side_effect = _exec_run
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    with pytest.raises(RuntimeError, match="unexpected container error"):
        asyncio.run(
            runner.run_preflight(
                name="preflight-checks",
                mount_path=_managed_mount(tmp_path),
                status_display=display,
            )
        )

    assert ("remove", "preflight-checks", "failed", "error") in display.calls


def test_agent_runner_run_preflight_renders_all_tests_green_when_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks",
            mount_path=_managed_mount(tmp_path),
            status_display=display,
        )
    )

    assert (
        "remove",
        "preflight-checks",
        "finished, all tests green",
        "success",
    ) in display.calls


def test_agent_runner_run_preflight_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    runner = AgentRunner({}, _make_cfg(tmp_path), mock_git, docker_client=MagicMock())

    with pytest.raises(GitCommandError):
        asyncio.run(
            runner.run_preflight(
                name="preflight-checks", mount_path=_managed_mount(tmp_path)
            )
        )


# ── RunRequest: core interface ────────────────────────────────────────────────


def test_run_request_stores_required_fields():
    from pycastle.agents.output_protocol import AgentRole

    req = _run_request(
        name="Agent",
        template=PromptTemplate.PLAN,
        mount_path=Path("/workspace"),
    )
    assert req.name == "Agent"
    assert req.prompt.template == PromptTemplate.PLAN
    assert req.mount_path == Path("/workspace")
    assert req.role == AgentRole.IMPLEMENTER
    assert req.prompt.scope_args == {
        "ALL_OPEN_ISSUES_JSON": "",
        "READY_FOR_AGENT_ISSUES_JSON": "",
    }
    assert req.model == ""
    assert req.effort == ""
    assert req.stage == ""
    assert req.token is None
    assert req.status_display is None
    assert req.issue_title == ""
    assert req.work_body == ""
    assert req.session_namespace == ""


def test_run_request_session_namespace_can_be_set():
    req = _run_request(
        name="Agent",
        template=PromptTemplate.PLAN,
        mount_path=Path("/workspace"),
        session_namespace="main",
    )
    assert req.session_namespace == "main"


# ── AgentRunner: ClaudeService pool integration ───────────────────────────────


def test_agent_runner_injects_picked_token_into_container_env(tmp_path):
    from pycastle.services.claude_service import ClaudeService

    captured_env: dict = {}
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def _run(*args, **kwargs):
        captured_env.update(kwargs.get("environment") or {})
        return mock_container

    mock_client.containers.run.side_effect = _run

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            r = MagicMock()
            r.output = iter(_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {"GH_TOKEN": "gh"},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
            )
        )
    )

    assert captured_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_filters_host_env_before_container_startup(tmp_path):
    started_envs: list[dict[str, str]] = []
    mock_client = MagicMock()
    mock_container = MagicMock()

    def _start_container(*args, **kwargs):
        started_envs.append(dict(kwargs.get("environment") or {}))
        return mock_container

    def _exec_run(*args, **kwargs):
        if kwargs.get("stream"):
            result = MagicMock()
            result.output = iter(_COMPLETE_STREAM)
            return result
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_client.containers.run.side_effect = _start_container
    mock_container.exec_run.side_effect = _exec_run

    runner = AgentRunner(
        {
            "GH_TOKEN": "gh-token",
            "PATH": r"C:\Windows\System32;C:\Windows",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary-token",
        },
        _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),)),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={
            "claude": ClaudeService(accounts=[("primary", "tok-primary")])
        },
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Implement",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPLEMENTER,
                service="claude",
            )
        )
    )
    asyncio.run(
        runner.run_preflight(name="preflight", mount_path=_managed_mount(tmp_path))
    )

    agent_env, preflight_env = started_envs
    assert agent_env["GH_TOKEN"] == "gh-token"
    assert "PATH" not in agent_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in agent_env
    assert agent_env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-primary"
    assert "CLAUDE_CONFIG_DIR" in agent_env

    assert preflight_env["GH_TOKEN"] == "gh-token"
    assert "PATH" not in preflight_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in preflight_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in preflight_env


def test_agent_runner_keeps_service_env_across_preflight_issue_review_and_failure_report(
    tmp_path,
    monkeypatch,
):
    started: list[tuple[str, dict[str, str]]] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    def _start_container(image_name: str, **kwargs):
        started.append((image_name, dict(kwargs.get("environment") or {})))
        return mock_container

    def _exec_run(cmd, **kwargs):
        del cmd
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_client.containers.run.side_effect = _start_container
    mock_container.exec_run.side_effect = _exec_run

    runner = AgentRunner(
        {
            "GH_TOKEN": "gh-token",
            "PATH": r"C:\Windows\System32;C:\Windows",
            "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "secondary-token",
            "OPENCODE_GO_API_KEY": "opencode-key",
        },
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={
            "claude": ClaudeService(accounts=[("primary", "tok-primary")]),
            "codex": CodexService(),
            "opencode": OpenCodeService(api_key="opencode-key"),
        },
    )
    runtime_client = _RuntimeSequenceClient(
        [
            _completed_outcome(
                output='<issue>{"number": 123, "labels": ["bug"]}</issue>',
                service="opencode",
                model="deepseek-v4-flash",
            ),
            _completed_outcome(
                output="<commit_message>done</commit_message>",
                service="codex",
                model="gpt-5.4",
            ),
            _completed_outcome(
                output='<issue>{"number": 456, "labels": ["bug"]}</issue>',
                service="claude",
                model="haiku",
            ),
        ]
    )
    monkeypatch.setattr(
        ContainerRunner,
        "_get_runtime_client",
        lambda self: runtime_client,
    )
    monkeypatch.setattr(ContainerRunner, "provider_argv_transform", lambda self: None)

    preflight_issue = asyncio.run(
        runner.run(
            _run_request(
                name="Host-Check Reporter",
                template=PromptTemplate.HOST_CHECK_ISSUE,
                scope_args={
                    "HOST_OS": "Windows",
                    "HOST_PLATFORM": "Windows-11",
                    "CHECKED_SHA": "abc123",
                    "CHECK_NAME": "pytest",
                    "COMMAND": "pytest tests/host",
                    "OUTPUT": "failed",
                },
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PREFLIGHT_ISSUE,
                service="opencode",
            )
        )
    )
    review = asyncio.run(
        runner.run(
            _run_request(
                name="Review",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.REVIEWER,
                service="codex",
            )
        )
    )
    failure_report = asyncio.run(
        runner.run(
            _run_request(
                name="Failure Report",
                template=PromptTemplate.FAILURE_REPORT,
                scope_args={
                    "SESSION_DIR": "/tmp/session",
                    "FAILED_ROLE": "reviewer",
                    "FAILURE_CLASS": "protocol_error",
                    "EVIDENCE_PATH": "",
                    "HAS_EVIDENCE_PATH": "no",
                },
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.FAILURE_REPORT,
                service="claude",
            )
        )
    )

    assert isinstance(preflight_issue, IssueOutput)
    assert isinstance(review, CommitMessageOutput)
    assert isinstance(failure_report, IssueOutput)

    opencode_env = started[0][1]
    codex_env = started[1][1]
    claude_env = started[2][1]

    assert started[0][0] == "pycastle-test"
    assert opencode_env["GH_TOKEN"] == "gh-token"
    assert opencode_env["OPENCODE_GO_API_KEY"] == "opencode-key"
    assert "OPENCODE_CONFIG_CONTENT" in opencode_env
    assert opencode_env["OPENCODE_HOME"].endswith("/.pycastle-session/preflight_issue")
    assert "PATH" not in opencode_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in opencode_env

    assert started[1][0] == "pycastle-test"
    assert codex_env["GH_TOKEN"] == "gh-token"
    assert codex_env["TZ"] == "UTC"
    assert codex_env["CODEX_HOME"].endswith("/.pycastle-session/reviewer")
    assert "PATH" not in codex_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in codex_env

    assert started[2][0] == "pycastle-test"
    assert claude_env["GH_TOKEN"] == "gh-token"
    assert claude_env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-primary"
    assert claude_env["CLAUDE_CONFIG_DIR"].endswith("/.pycastle-session/failure_report")
    assert "PATH" not in claude_env
    assert "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY" not in claude_env


def test_agent_runner_cancels_token_and_raises_on_transient_agent_error(tmp_path):
    """TransientAgentError from a 5xx result cancels the CancellationToken and re-raises."""
    token = CancellationToken()
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.RuntimeOutcome(
                kind=agent_runtime.runtime.ProviderUnavailable(
                    reason=agent_runtime.errors.ProviderUnavailableReason.TRANSIENT_API_ERROR,
                    detail="API Error: 529 Overloaded",
                ),
                result=agent_runtime.runtime.RunResult(
                    output="",
                    usage=None,
                    continuation=None,
                    selected=agent_runtime.types.ResolvedProvider(
                        service="claude",
                        model="haiku",
                        effort="medium",
                    ),
                ),
            )
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        with pytest.raises(TransientAgentError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Implement Agent #42",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=_managed_mount(tmp_path),
                        token=token,
                    )
                )
            )

    assert token.is_cancelled


def test_agent_runner_does_not_call_mark_exhausted_on_transient_agent_error(tmp_path):
    """TransientAgentError must NOT mark the account exhausted (server-wide, not account-specific)."""
    from pycastle.services.claude_service import ClaudeService

    svc = ClaudeService(accounts=[("primary", "tok-primary")])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"claude": svc},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.RuntimeOutcome(
                kind=agent_runtime.runtime.ProviderUnavailable(
                    reason=agent_runtime.errors.ProviderUnavailableReason.TRANSIENT_API_ERROR,
                    detail="API Error: 529 Overloaded",
                ),
                result=agent_runtime.runtime.RunResult(
                    output="",
                    usage=None,
                    continuation=None,
                    selected=agent_runtime.types.ResolvedProvider(
                        service="claude",
                        model="haiku",
                        effort="medium",
                    ),
                ),
            )
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        with pytest.raises(TransientAgentError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=_managed_mount(tmp_path),
                    )
                )
            )

    # Account must still be available — mark_exhausted was NOT called
    assert svc.is_available() is True


def test_agent_runner_marks_picked_token_exhausted_on_usage_limit(tmp_path):
    from pycastle.services.claude_service import ClaudeService

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"claude": svc},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.RuntimeOutcome(
                kind=agent_runtime.runtime.UsageLimited(reset_time=None),
                result=agent_runtime.runtime.RunResult(
                    output="",
                    usage=None,
                    continuation=None,
                    selected=agent_runtime.types.ResolvedProvider(
                        service="claude",
                        model="haiku",
                        effort="medium",
                    ),
                ),
            )
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        with pytest.raises(UsageLimitError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=_managed_mount(tmp_path),
                    )
                )
            )

    # secondary was picked (highest priority) and marked exhausted; primary should now be available
    assert svc.is_available() is True
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-primary"


def test_agent_runner_routes_subscription_access_denial_to_agent_credential_failure(
    tmp_path,
):
    from pycastle.services.claude_service import ClaudeService

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"claude": svc},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.errors.AgentCredentialFailureError(
                "Claude subscription access disabled.",
                service_name="claude",
            )
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        with pytest.raises(AgentCredentialFailureError) as exc_info:
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=_managed_mount(tmp_path),
                    )
                )
            )

    assert exc_info.value.service_name == "claude"
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_routes_subscription_access_denial_variant_to_agent_credential_failure(
    tmp_path,
):
    from pycastle.services.claude_service import ClaudeService

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"claude": svc},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.errors.AgentCredentialFailureError(
                "Claude subscription access disabled.",
                service_name="claude",
            )
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        with pytest.raises(AgentCredentialFailureError) as exc_info:
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=_managed_mount(tmp_path),
                    )
                )
            )

    assert exc_info.value.service_name == "claude"
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_rotates_opencode_after_invalid_key_and_succeeds_with_next_credential(
    tmp_path,
):
    from pycastle.services.opencode_service import OpenCodeService

    svc = OpenCodeService(
        accounts=[("account 1", "invalid-key"), ("account 2", "valid-key")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"opencode": svc},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.errors.AgentCredentialFailureError(
                "invalid api key",
                service_name="opencode",
            ),
            _completed_outcome(
                output="<commit_message>done</commit_message>",
                service="opencode",
                model="deepseek-v4-flash",
            ),
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Test Reviewer",
                    template=PromptTemplate.REVIEW,
                    scope_args={
                        "ISSUE_NUMBER": "77",
                        "ISSUE_TITLE": "Doc bug",
                        "ISSUE_BODY": "Body",
                        "ISSUE_COMMENTS": "",
                        "BRANCH": "issue-77-docs",
                        "INTERRUPTED_WORK": "",
                    },
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.REVIEWER,
                    service="opencode",
                )
            )
        )

    assert result is not None
    env = svc.build_env()
    assert env["OPENCODE_GO_API_KEY"] == "valid-key"


def test_agent_runner_opencode_invalid_key_exhausts_last_credential_and_surfaces_failure(
    tmp_path,
):
    from pycastle.services.opencode_service import OpenCodeService

    svc = OpenCodeService(accounts=[("account 1", "invalid-key")])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"opencode": svc},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.errors.AgentCredentialFailureError(
                "invalid api key",
                service_name="opencode",
            )
        ]
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_runtime_runner(monkeypatch, runner, runtime_client)

        with pytest.raises(AgentCredentialFailureError) as exc_info:
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test Reviewer",
                        template=PromptTemplate.REVIEW,
                        scope_args={
                            "ISSUE_NUMBER": "77",
                            "ISSUE_TITLE": "Doc bug",
                            "ISSUE_BODY": "Body",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-77-docs",
                            "INTERRUPTED_WORK": "",
                        },
                        mount_path=_managed_mount(tmp_path),
                        role=AgentRole.REVIEWER,
                        service="opencode",
                    )
                )
            )

    assert exc_info.value.service_name == "opencode"


def test_agent_runner_keeps_invalid_opencode_credential_retired_after_another_credential_resets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timezone

    from pycastle.services.opencode_service import OpenCodeService

    svc = OpenCodeService(
        accounts=[("account 1", "invalid-key"), ("account 2", "valid-key")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_COMPLETE_STREAM),
        service_registry={"opencode": svc},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.errors.AgentCredentialFailureError(
                "invalid api key",
                service_name="opencode",
            ),
            agent_runtime.RuntimeOutcome(
                kind=agent_runtime.runtime.UsageLimited(
                    reset_time=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
                ),
                result=agent_runtime.runtime.RunResult(
                    output="",
                    usage=None,
                    continuation=None,
                    selected=agent_runtime.types.ResolvedProvider(
                        service="opencode",
                        model="deepseek-v4-flash",
                        effort="medium",
                    ),
                ),
            ),
        ]
    )
    with pytest.MonkeyPatch.context() as runtime_monkeypatch:
        _patch_runtime_runner(runtime_monkeypatch, runner, runtime_client)

        with pytest.raises(UsageLimitError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test Reviewer",
                        template=PromptTemplate.REVIEW,
                        scope_args={
                            "ISSUE_NUMBER": "77",
                            "ISSUE_TITLE": "Doc bug",
                            "ISSUE_BODY": "Body",
                            "ISSUE_COMMENTS": "",
                            "BRANCH": "issue-77-docs",
                            "INTERRUPTED_WORK": "",
                        },
                        mount_path=_managed_mount(tmp_path),
                        role=AgentRole.REVIEWER,
                        service="opencode",
                    )
                )
            )

    later = datetime(2026, 6, 25, 12, 5, tzinfo=timezone.utc).astimezone()
    monkeypatch.setattr(
        "pycastle.services.credential_pool._time_module.now_local",
        lambda: later,
    )

    env = svc.build_env()

    assert env["OPENCODE_GO_API_KEY"] == "valid-key"


def test_agent_runner_treats_unrelated_403_as_hard_error(tmp_path):
    mock_client = _make_docker_client(
        [
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": 403,
                    "result": "Forbidden: your IP address is not allowed.",
                }
            ).encode()
            + b"\n"
        ]
    )
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(HardAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                )
            )
        )


def test_agent_runner_codex_missing_host_auth_fails_before_container_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    docker_client = _make_docker_client([])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=docker_client,
        service_registry={"codex": CodexService()},
    )
    runtime_client = _RuntimeSequenceClient(
        [
            agent_runtime.errors.AgentCredentialFailureError(
                "Codex authentication missing: run `codex login` on the host.",
                service_name="codex",
            )
        ]
    )
    _patch_runtime_runner(monkeypatch, runner, runtime_client)

    with pytest.raises(AgentCredentialFailureError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Codex",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service="codex",
                )
            )
        )

    assert exc_info.value.service_name == "codex"


def test_fake_agent_runner_accepts_run_request_and_records_it():
    completion = CompletionOutput()
    fake = FakeAgentRunner([completion])
    req = _run_request(
        name="Planner",
        template=_PLAN_TEMPLATE,
        mount_path=Path("/w"),
    )
    result = asyncio.run(fake.run(req))
    assert result is completion
    assert fake.calls[0] is req


def _seed_implementer_session(tmp_path: Path) -> None:
    """Seed the claude service state dir so ClaudeService.is_resumable returns True."""
    claude_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "session.json").write_text("{}")


def test_work_invocation_resume_non_typed_retry_raises_agent_failed_error(
    tmp_path: Path,
):
    _seed_implementer_session(tmp_path)
    status_display = RecordingStatusDisplay()
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> CommitMessageOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            raise RuntimeError(f"boom-{len(work_calls)}")

    service = ClaudeService()
    session = _FakeSession()
    runner = _FakeRunner()
    expected_session_id = _role_session_session_uuid(
        RoleSession(tmp_path, AgentRole.IMPLEMENTER, "")
    )

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        return "resume"

    with pytest.raises(AgentFailedError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Impl",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.IMPLEMENTER,
                    service=service,
                    model="sonnet",
                    effort="high",
                    output_adapter=ProtocolOutputAdapter(
                        prompt_factory=prompt_factory,
                        reprompt_message="reprompt",
                    ),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: _PreparedRunSessionStandIn(
                            initial_run_kind=RunKind.RESUME,
                            initial_provider_session_id=expected_session_id,
                        ),
                        build_session=lambda *_args: session,
                        build_runner=lambda *_args: cast(ContainerRunner, runner),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                    allow_non_typed_resume_retry=True,
                )
            )
        )

    assert exc_info.value.failure_class == "non_typed_crash"
    assert exc_info.value.service_name == "claude"
    assert work_calls == [
        (RunKind.RESUME, expected_session_id, "resume"),
        (RunKind.RESUME, expected_session_id, "resume"),
    ]
    assert ("remove", "Impl", "failed", "error") in status_display.calls


def test_work_invocation_typed_fresh_success_returns_adapter_output_and_forwarded_prompt(
    tmp_path: Path,
):
    status_display = RecordingStatusDisplay()
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        provider_state_dir_container_path="/workspace/provider-state",
    )
    session_state_dirs: list[str | None] = []
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            return PlannerOutput(issues=[])

    service = ClaudeService()
    session = _FakeSession()
    runner = _FakeRunner()

    def build_session(
        _mount_path: Path, _service, state_dir: str | None
    ) -> _FakeSession:
        session_state_dirs.append(state_dir)
        return session

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.FRESH
        return "caller-rendered prompt text"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                service=service,
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=build_session,
                    build_runner=lambda *_args: cast(ContainerRunner, runner),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
                status_display=status_display,
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert work_calls == [
        (RunKind.FRESH, "provider-fresh", "caller-rendered prompt text")
    ]
    assert session_state_dirs == ["/workspace/provider-state"]
    assert prepared_session.prepare_for_run_calls == 1
    assert prepared_session.initial_session.successful_run_calls == 1


def test_work_invocation_wraps_setup_docker_error_and_skips_work_adapter(
    tmp_path: Path,
):
    status_display = RecordingStatusDisplay()
    adapter_calls: list[str] = []
    exit_calls: list[tuple[object, object, object]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            exit_calls.append((exc_type, exc, tb))
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body
            raise DockerError("dependency install failed")

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            adapter_calls.append("work")
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        adapter_calls.append("build_prompt")
        return "caller-rendered prompt text"

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=ProtocolOutputAdapter(
                        prompt_factory=prompt_factory,
                        reprompt_message="reprompt",
                    ),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: _PreparedRunSessionStandIn(
                            initial_run_kind=RunKind.FRESH,
                            initial_provider_session_id=None,
                        ),
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                        translate_setup_failure=lambda role, exc: (
                            SetupPhaseError(role.value, str(exc))
                            if isinstance(exc, DockerError)
                            else None
                        ),
                    ),
                    status_display=status_display,
                )
            )
        )

    assert exc_info.value.phase == AgentRole.PLANNER.value
    assert str(exc_info.value) == "dependency install failed"
    assert adapter_calls == []
    assert exit_calls == [(None, None, None)]
    assert ("remove", "Planner", "failed", "error") in status_display.calls


def test_work_invocation_passes_runtime_run_session_plan_to_prepare_session(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        provider_state_dir_container_path="/workspace/provider-state",
    )
    observed_plan: RuntimeRunSessionPlan | None = None

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: AgentToolPolicyGroup = AgentToolPolicyGroup.FULL,
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id=None,
        ) -> str:
            del role, tool_policy, run_kind, session_uuid, on_provider_session_id
            return prompt

    def _prepare_session(
        run_session_plan: RuntimeRunSessionPlan,
    ) -> _PreparedRunSessionStandIn:
        nonlocal observed_plan
        observed_plan = run_session_plan
        return prepared_session

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Runtime Consumer",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPLEMENTER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=_stage_key_for_role,
                    prepare_session=_prepare_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert result == "already-rendered prompt"
    assert observed_plan is not None
    assert observed_plan.mount_path == _managed_mount(tmp_path)
    assert observed_plan.role is AgentRole.IMPLEMENTER
    assert observed_plan.session_namespace == ""
    assert observed_plan.container_workspace == "/home/agent/workspace"
    assert observed_plan.run_session_plan is None
    assert isinstance(observed_plan.service, ClaudeService)


@pytest.mark.parametrize(
    ("seed_resumable_state", "expected_run_kind"),
    [
        (False, RunKind.FRESH),
        (True, RunKind.RESUME),
    ],
)
def test_agent_runner_prepare_session_accepts_runtime_provider_run_state_plan_for_fresh_and_resume(
    tmp_path: Path,
    seed_resumable_state: bool,
    expected_run_kind: RunKind,
):
    if seed_resumable_state:
        state_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
        state_dir.mkdir(parents=True)
        (state_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    service = ClaudeService()
    provider_run_state_plan = plan_provider_run_state(
        _provider_run_state_plan_request(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
        )
    )
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    dependencies = runner.build_work_dependencies(
        name="Runtime Consumer",
        model="sonnet",
        effort="high",
        service=service,
    )

    prepared_session = cast(
        Any,
        dependencies.prepare_session(
            RuntimeRunSessionPlan(
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPLEMENTER,
                session_namespace="",
                service=service,
                container_workspace="/home/agent/workspace",
                run_session_plan=provider_run_state_plan,
            )
        ),
    )

    assert prepared_session.run_kind is expected_run_kind
    assert (
        prepared_session.provider_session_id
        == provider_run_state_plan.provider_session_id
    )


def test_agent_runner_prepare_session_restarts_strict_resume_when_exact_transcript_identity_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    auth_path = home / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text("{}", encoding="utf-8")

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "06" / "16"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-recovered"}\n',
        encoding="utf-8",
    )

    service = CodexService()
    provider_run_state_plan = plan_provider_run_state(
        _provider_run_state_plan_request(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
        )
    )
    runner = AgentRunner(
        {"CODEX_HOME": str(home / ".codex")},
        _make_cfg(tmp_path),
        _make_git_service(),
        service_registry={"codex": service},
    )
    dependencies = runner.build_work_dependencies(
        name="Runtime Consumer",
        model="gpt-5.4",
        effort="medium",
        service=service,
    )

    prepared_session = cast(
        Any,
        dependencies.prepare_session(
            RuntimeRunSessionPlan(
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPLEMENTER,
                session_namespace="",
                service=service,
                container_workspace="/home/agent/workspace",
                run_session_plan=provider_run_state_plan,
            )
        ),
    )
    strict_resume = prepared_session.resumable_provider_run_session()

    assert prepared_session.run_kind is RunKind.RESUME
    assert prepared_session.provider_session_id == "thread-recovered"
    assert provider_run_state_plan.exact_transcript_match is False
    assert strict_resume.run_kind is RunKind.FRESH
    assert strict_resume.provider_session_id is None


def test_agent_runner_prepare_session_keeps_ambiguous_codex_recovery_fresh_for_ordinary_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    auth_path = home / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text("{}", encoding="utf-8")

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    dir_a = state_dir / "sessions" / "2026" / "06" / "16"
    dir_b = state_dir / "sessions" / "2026" / "06" / "17"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-alpha"}\n',
        encoding="utf-8",
    )
    (dir_b / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-beta"}\n',
        encoding="utf-8",
    )

    service = CodexService()
    provider_run_state_plan = plan_provider_run_state(
        _provider_run_state_plan_request(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
        )
    )
    runner = AgentRunner(
        {"CODEX_HOME": str(home / ".codex")},
        _make_cfg(tmp_path),
        _make_git_service(),
        service_registry={"codex": service},
    )
    dependencies = runner.build_work_dependencies(
        name="Runtime Consumer",
        model="gpt-5.4",
        effort="medium",
        service=service,
    )

    prepared_session = cast(
        Any,
        dependencies.prepare_session(
            RuntimeRunSessionPlan(
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPLEMENTER,
                session_namespace="",
                service=service,
                container_workspace="/home/agent/workspace",
                run_session_plan=provider_run_state_plan,
            )
        ),
    )
    initial_run = prepared_session.initial_provider_run_session()

    assert provider_run_state_plan.run_kind is RunKind.FRESH
    assert provider_run_state_plan.provider_session_id is None
    assert prepared_session.run_kind is RunKind.FRESH
    assert prepared_session.provider_session_id is None
    assert initial_run.run_kind is RunKind.FRESH
    assert initial_run.provider_session_id is None


def test_agent_runner_prepare_session_accepts_runtime_provider_run_state_plan_for_strict_resume_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    auth_path = home / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text("{}", encoding="utf-8")

    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions" / "2026" / "06" / "16"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-recovered"}\n',
        encoding="utf-8",
    )

    service = CodexService()
    provider_run_state_plan = plan_provider_run_state(
        _provider_run_state_plan_request(
            worktree=tmp_path,
            role=AgentRole.IMPLEMENTER,
            namespace="",
            service=service,
        )
    )
    runner = AgentRunner(
        {"CODEX_HOME": str(home / ".codex")},
        _make_cfg(tmp_path),
        _make_git_service(),
        service_registry={"codex": service},
    )
    dependencies = runner.build_work_dependencies(
        name="Runtime Consumer",
        model="gpt-5.4",
        effort="medium",
        service=service,
    )

    prepared_session = cast(
        Any,
        dependencies.prepare_session(
            RuntimeRunSessionPlan(
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPLEMENTER,
                session_namespace="",
                service=service,
                container_workspace="/home/agent/workspace",
                run_session_plan=provider_run_state_plan,
            )
        ),
    )
    strict_resume = prepared_session.resumable_provider_run_session()

    assert prepared_session.run_kind is RunKind.RESUME
    assert prepared_session.provider_session_id == "thread-recovered"
    assert strict_resume.run_kind is RunKind.FRESH
    assert strict_resume.provider_session_id is None


def test_work_invocation_wraps_configured_setup_error_and_skips_work_adapter(
    tmp_path: Path,
):
    status_display = RecordingStatusDisplay()
    adapter_calls: list[str] = []

    class _CustomSetupError(RuntimeError):
        pass

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body
            raise _CustomSetupError("custom setup failed")

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: AgentToolPolicyGroup = AgentToolPolicyGroup.FULL,
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id=None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            adapter_calls.append("work_text")
            return "unexpected"

    setup_error = SetupPhaseError(AgentRole.IMPLEMENTER.value, "custom setup failed")

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.IMPLEMENTER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=_stage_key_for_role,
                        prepare_session=lambda _request: _PreparedRunSessionStandIn(
                            initial_run_kind=RunKind.FRESH,
                            initial_provider_session_id=None,
                        ),
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                        translate_setup_failure=lambda role, exc: (
                            SetupPhaseError(role.value, str(exc))
                            if isinstance(exc, _CustomSetupError)
                            else None
                        ),
                    ),
                    status_display=status_display,
                )
            )
        )

    assert exc_info.value.phase == setup_error.phase
    assert str(exc_info.value) == str(setup_error)
    assert adapter_calls == []
    assert ("remove", "Runtime Consumer", "failed", "error") in status_display.calls


def test_work_invocation_opens_status_row_with_caller_metadata_before_setup(
    tmp_path: Path,
):
    status_display = RecordingStatusDisplay()
    observed_events: list[str] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email
            observed_events.append("setup")
            assert status_display.register_calls[0] == {
                "caller": "Planner",
                "kind": "agent",
                "startup_message": "started",
                "work_body": "implement feature slice",
                "initial_phase": "Setup",
                "color_key": 7,
                "model_display": ModelDisplayMetadata(
                    service="claude",
                    model="sonnet",
                    effort="high",
                ),
            }
            assert work_body == "implement feature slice"

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            observed_events.append("work")
            assert len(status_display.register_calls) == 1
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        observed_events.append("build_prompt")
        return "caller-rendered prompt text"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: _PreparedRunSessionStandIn(
                        initial_run_kind=RunKind.FRESH,
                        initial_provider_session_id=None,
                    ),
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                    build_model_display_metadata=lambda service, model, effort: (
                        ModelDisplayMetadata(
                            service=service,
                            model=model,
                            effort=effort,
                        )
                    ),
                ),
                status_display=status_display,
                work_body="implement feature slice",
                color_key=7,
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert observed_events == ["setup", "build_prompt", "work"]


def test_work_invocation_pre_cancelled_token_raises_usage_limit_before_setup(
    tmp_path: Path,
):
    token = CancellationToken()
    token.cancel()
    observed_calls: list[str] = []
    status_display = RecordingStatusDisplay()

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body
            observed_calls.append("setup")

    def _prepare_session(_request: object) -> _PreparedRunSessionStandIn:
        observed_calls.append("prepare_session")
        return _PreparedRunSessionStandIn(
            initial_run_kind=RunKind.FRESH,
            initial_provider_session_id=None,
        )

    def _build_session(*_args: object) -> _FakeSession:
        observed_calls.append("build_session")
        return _FakeSession()

    def _build_runner(*_args: object) -> ContainerRunner:
        observed_calls.append("build_runner")
        return cast(ContainerRunner, _FakeRunner())

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.IMPLEMENTER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=_stage_key_for_role,
                        prepare_session=_prepare_session,
                        build_session=_build_session,
                        build_runner=_build_runner,
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                    token=token,
                )
            )
        )

    assert exc_info.value.stage_key == "implement"
    assert observed_calls == []
    assert status_display.register_calls == []


def test_work_invocation_sets_missing_stage_key_on_provider_usage_limit(
    tmp_path: Path,
):
    class _TrackingService(_RecordingAgentService):
        def __init__(self) -> None:
            super().__init__("codex")
            self.mark_exhausted_calls: list[datetime | None] = []

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

    service = _TrackingService()
    reset_time = datetime(2026, 6, 8, 12, 0, 0)

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            raise UsageLimitError(reset_time=reset_time)

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        return "caller-rendered prompt text"

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=service,
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=ProtocolOutputAdapter(
                        prompt_factory=prompt_factory,
                        reprompt_message="reprompt",
                    ),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=_stage_key_for_role,
                        prepare_session=lambda _request: _PreparedRunSessionStandIn(
                            initial_run_kind=RunKind.FRESH,
                            initial_provider_session_id=None,
                        ),
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                )
            )
        )

    assert exc_info.value.stage_key == "plan"
    assert service.mark_exhausted_calls == [reset_time]


def test_work_invocation_text_usage_limit_marks_exhaustion_cancels_token_and_skips_success_metadata(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
    )
    token = CancellationToken()
    reset_time = datetime(2026, 6, 8, 12, 30, 0)

    class _TrackingService(_RecordingAgentService):
        def __init__(self) -> None:
            super().__init__("codex")
            self.mark_exhausted_calls: list[datetime | None] = []

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

    service = _TrackingService()

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del prompt, role, tool_policy, run_kind, session_uuid
            assert on_provider_session_id is not None
            on_provider_session_id("provider-runtime")
            raise UsageLimitError(reset_time=reset_time)

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.IMPLEMENTER,
                    service=service,
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=_stage_key_for_role,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                        handle_provider_account_exhaustion=lambda service_for_run, err: (
                            setattr(
                                err,
                                "account_label",
                                service_for_run.mark_permanently_exhausted(),
                            )
                            if err.is_permanent
                            and isinstance(service_for_run, ClaudeService)
                            else service_for_run.mark_exhausted(err.reset_time)
                        ),
                    ),
                    token=token,
                )
            )
        )

    assert exc_info.value.stage_key == "implement"
    assert token.is_cancelled
    assert service.mark_exhausted_calls == [reset_time]
    assert prepared_session.initial_session.recorded_provider_session_ids == [
        "provider-runtime"
    ]
    assert prepared_session.initial_session.successful_run_calls == 0


def test_work_invocation_text_transient_provider_failure_cancels_token_keeps_service_available_logs_status_and_skips_success_metadata(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
    )
    status_display = RecordingStatusDisplay()
    token = CancellationToken()

    class _TrackingService(_RecordingAgentService):
        def __init__(self) -> None:
            super().__init__("codex")
            self.mark_exhausted_calls: list[datetime | None] = []

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

    service = _TrackingService()

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del prompt, role, tool_policy, run_kind, session_uuid
            assert on_provider_session_id is not None
            on_provider_session_id("provider-runtime")
            raise TransientAgentError(status_code=529)

    with pytest.raises(TransientAgentError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.IMPLEMENTER,
                    service=service,
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=_stage_key_for_role,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                        transient_status_message=format_transient_status_message,
                    ),
                    status_display=status_display,
                    token=token,
                )
            )
        )

    assert exc_info.value.status_code == 529
    assert token.is_cancelled
    assert service.mark_exhausted_calls == []
    assert ("print", "Runtime Consumer", "transient API error: status 529", None) in (
        status_display.calls
    )
    assert prepared_session.initial_session.recorded_provider_session_ids == [
        "provider-runtime"
    ]
    assert prepared_session.initial_session.successful_run_calls == 0


def test_work_invocation_text_hard_provider_failure_cancels_token_annotates_context_and_skips_success_metadata(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
    )
    token = CancellationToken()

    class _TrackingService(_RecordingAgentService):
        def __init__(self) -> None:
            super().__init__("codex")
            self.mark_exhausted_calls: list[datetime | None] = []

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

    service = _TrackingService()

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del prompt, role, tool_policy, run_kind, session_uuid
            assert on_provider_session_id is not None
            on_provider_session_id("provider-runtime")
            raise HardAgentError("provider rejected request", status_code=403)

    with pytest.raises(HardAgentError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.IMPLEMENTER,
                    service=service,
                    model="gpt-5.4",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=_stage_key_for_role,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                        handle_provider_account_exhaustion=lambda service_for_run, err: (
                            setattr(
                                err,
                                "account_label",
                                service_for_run.mark_permanently_exhausted(),
                            )
                            if err.is_permanent
                            and isinstance(service_for_run, ClaudeService)
                            else service_for_run.mark_exhausted(err.reset_time)
                        ),
                    ),
                    token=token,
                )
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.caller == "Runtime Consumer"
    assert exc_info.value.service_name == "codex"
    assert token.is_cancelled
    assert service.mark_exhausted_calls == []
    assert prepared_session.initial_session.recorded_provider_session_ids == [
        "provider-runtime"
    ]
    assert prepared_session.initial_session.successful_run_calls == 0


def test_work_invocation_permanent_claude_usage_limit_marks_account_and_skips_success_metadata(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
    )
    token = CancellationToken()

    class _TrackingClaudeService(ClaudeService):
        def __init__(self) -> None:
            super().__init__()
            self.mark_exhausted_calls: list[datetime | None] = []
            self.mark_permanently_exhausted_calls = 0

        def mark_exhausted(
            self, reset_time: datetime | None, *, _now: datetime | None = None
        ) -> None:
            del _now
            self.mark_exhausted_calls.append(reset_time)

        def mark_permanently_exhausted(self) -> str | None:
            self.mark_permanently_exhausted_calls += 1
            return "secondary"

    service = _TrackingClaudeService()

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, prompt, run_kind, session_uuid
            assert on_provider_session_id is not None
            on_provider_session_id("provider-runtime")
            raise UsageLimitError(reset_time=None, is_permanent=True)

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        return "caller-rendered prompt text"

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=service,
                    model="sonnet",
                    effort="high",
                    output_adapter=ProtocolOutputAdapter(
                        prompt_factory=prompt_factory,
                        reprompt_message="reprompt",
                    ),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=_stage_key_for_role,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                        handle_provider_account_exhaustion=lambda service_for_run, err: (
                            setattr(
                                err,
                                "account_label",
                                service_for_run.mark_permanently_exhausted(),
                            )
                            if err.is_permanent
                            and isinstance(service_for_run, ClaudeService)
                            else service_for_run.mark_exhausted(err.reset_time)
                        ),
                    ),
                    token=token,
                )
            )
        )

    assert exc_info.value.stage_key == "plan"
    assert exc_info.value.account_label == "secondary"
    assert token.is_cancelled
    assert service.mark_permanently_exhausted_calls == 1
    assert service.mark_exhausted_calls == []
    assert prepared_session.initial_session.recorded_provider_session_ids == [
        "provider-runtime"
    ]
    assert prepared_session.initial_session.successful_run_calls == 0


def test_work_invocation_exits_container_session_once_across_work_outcomes(
    tmp_path: Path,
):
    credential_error = AgentCredentialFailureError(
        "credential failure",
        service_name="claude",
    )
    hard_error = HardAgentError(
        "hard failure",
        service_name="claude",
    )
    scenarios = [
        ("success", "typed", PlannerOutput(issues=[]), None),
        ("failed_output", "typed", FailedOutput(failure_class="agent_failed"), None),
        ("setup_failure", "typed", None, DockerError("setup failed")),
        ("timeout_failure", "text", None, AgentTimeoutError("timeout")),
        ("usage_limit_failure", "text", None, UsageLimitError(stage_key="plan")),
        ("transient_failure", "text", None, TransientAgentError("transient")),
        ("hard_failure", "text", None, hard_error),
        ("credential_failure", "text", None, credential_error),
    ]

    for scenario_name, mode, result, error in scenarios:
        status_display = RecordingStatusDisplay()
        exit_calls: list[tuple[object, object, object]] = []

        class _FakeSession:
            def exec_simple(self, cmd: str) -> str:
                raise AssertionError(
                    f"{scenario_name}: unexpected container exec: {cmd}"
                )

            def __exit__(self, exc_type, exc, tb) -> None:
                exit_calls.append((exc_type, exc, tb))
                return None

        class _FakeRunner:
            async def setup(
                self, git_name: str, git_email: str, work_body: str
            ) -> None:
                del git_name, git_email, work_body
                if scenario_name == "setup_failure":
                    raise cast(DockerError, error)

            async def work(
                self,
                role: AgentRole,
                prompt: str,
                *,
                run_kind: RunKind,
                session_uuid: str | None,
                on_provider_session_id=None,
            ) -> PlannerOutput | FailedOutput:
                del role, prompt, run_kind, session_uuid, on_provider_session_id
                if error is not None:
                    raise error
                assert result is not None
                return cast(PlannerOutput | FailedOutput, result)

            async def work_text(
                self,
                prompt: str,
                *,
                role: AgentRole,
                tool_policy,
                run_kind: RunKind,
                session_uuid: str | None,
                on_provider_session_id=None,
            ) -> str:
                del (
                    prompt,
                    role,
                    tool_policy,
                    run_kind,
                    session_uuid,
                    on_provider_session_id,
                )
                assert error is not None
                raise error

        output_adapter: ProtocolOutputAdapter | TextOutputAdapter
        if mode == "typed":
            output_adapter = ProtocolOutputAdapter(
                prompt_factory=lambda **_kwargs: asyncio.sleep(
                    0, result="caller-rendered prompt text"
                ),
                reprompt_message="reprompt",
            )
        else:
            output_adapter = TextOutputAdapter(prompt="text prompt")

        request: WorkInvocationRequest[Any] = WorkInvocationRequest(
            name=f"Planner {scenario_name}",
            mount_path=_managed_mount(tmp_path),
            role=AgentRole.PLANNER,
            service=ClaudeService(),
            model="sonnet",
            effort="high",
            output_adapter=output_adapter,
            dependencies=WorkInvocationDependencies(
                container_workspace="/home/agent/workspace",
                timeout_retries=0,
                stage_key_for_role=lambda role: role.value,
                prepare_session=lambda _request: _PreparedRunSessionStandIn(
                    initial_run_kind=RunKind.FRESH,
                    initial_provider_session_id=None,
                ),
                build_session=lambda *_args: _FakeSession(),
                build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                get_git_identity=lambda: ("Test User", "test@example.com"),
                translate_setup_failure=lambda role, exc: (
                    SetupPhaseError(role.value, str(exc))
                    if isinstance(exc, DockerError)
                    else None
                ),
            ),
            status_display=status_display,
        )

        if scenario_name == "success":
            observed_result = asyncio.run(invoke_work(request))
            assert observed_result == PlannerOutput(issues=[])
        elif scenario_name == "failed_output":
            with pytest.raises(AgentFailedError):
                asyncio.run(invoke_work(request))
        elif scenario_name == "setup_failure":
            with pytest.raises(SetupPhaseError):
                asyncio.run(invoke_work(request))
        else:
            with pytest.raises(type(cast(BaseException, error))):
                asyncio.run(invoke_work(request))

        assert exit_calls == [(None, None, None)]
        assert len(status_display.remove_calls) == 1


@pytest.mark.parametrize(
    ("mode", "expected_exception"),
    [
        ("typed_failed_output", AgentFailedError),
        ("text_protocol_error", AgentOutputProtocolError),
    ],
)
def test_work_invocation_closes_failed_work_rows_consistently_for_typed_and_text_runs(
    tmp_path: Path,
    mode: str,
    expected_exception: type[BaseException],
):
    status_display = RecordingStatusDisplay()

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        def __init__(self, display: RecordingStatusDisplay) -> None:
            self._display = display

        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> FailedOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            self._display.update_phase("Planner", "Work")
            return FailedOutput(failure_class="agent_failed")

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            self._display.update_phase("Planner", "Work")
            raise AgentOutputProtocolError("text outputs do not reprompt")

    output_adapter: ProtocolOutputAdapter | TextOutputAdapter
    if mode == "typed_failed_output":
        output_adapter = ProtocolOutputAdapter(
            prompt_factory=lambda **_kwargs: asyncio.sleep(
                0, result="caller-rendered prompt text"
            ),
            reprompt_message="reprompt",
        )
    else:
        output_adapter = TextOutputAdapter(prompt="text prompt")

    with pytest.raises(expected_exception):
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=output_adapter,
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: _PreparedRunSessionStandIn(
                            initial_run_kind=RunKind.FRESH,
                            initial_provider_session_id=None,
                        ),
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner(status_display)
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                )
            )
        )

    assert status_display.phase_updates == [("Planner", "Work")]
    assert status_display.remove_calls == [
        {
            "caller": "Planner",
            "shutdown_message": "failed",
            "shutdown_style": "error",
        }
    ]


def test_work_invocation_typed_resume_success_uses_prepared_run_kind_and_provider_session_id(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.RESUME,
        initial_provider_session_id="provider-resume",
    )
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.RESUME
        return "resume prompt from adapter"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert work_calls == [
        (RunKind.RESUME, "provider-resume", "resume prompt from adapter")
    ]
    assert prepared_session.initial_session.successful_run_calls == 1


@pytest.mark.parametrize(
    ("initial_run_kind", "initial_provider_session_id", "observed_provider_session_id"),
    [
        (RunKind.FRESH, "provider-fresh", "provider-fresh-runtime"),
        (RunKind.RESUME, "provider-resume", "provider-resume-runtime"),
    ],
)
def test_work_invocation_text_success_returns_exact_str_and_records_provider_session_metadata(
    tmp_path: Path,
    initial_run_kind: RunKind,
    initial_provider_session_id: str,
    observed_provider_session_id: str,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=initial_run_kind,
        initial_provider_session_id=initial_provider_session_id,
    )
    work_calls: list[
        tuple[str, AgentRole, AgentToolPolicyGroup, RunKind, str | None]
    ] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole = AgentRole.IMPLEMENTER,
            tool_policy: AgentToolPolicyGroup = AgentToolPolicyGroup.FULL,
            run_kind: RunKind = RunKind.FRESH,
            session_uuid: str | None = None,
            on_provider_session_id=None,
        ) -> str:
            work_calls.append((prompt, role, tool_policy, run_kind, session_uuid))
            assert on_provider_session_id is not None
            on_provider_session_id(observed_provider_session_id)
            return "exact text from adapter"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Runtime Consumer",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPLEMENTER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=TextOutputAdapter(
                    prompt="already-rendered prompt text",
                    tool_policy=AgentToolPolicyGroup.PARTIAL,
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert result == "exact text from adapter"
    assert work_calls == [
        (
            "already-rendered prompt text",
            AgentRole.IMPLEMENTER,
            AgentToolPolicyGroup.PARTIAL,
            initial_run_kind,
            initial_provider_session_id,
        )
    ]
    assert prepared_session.initial_session.recorded_provider_session_ids == [
        observed_provider_session_id
    ]
    assert prepared_session.initial_session.provider_session_id == (
        observed_provider_session_id
    )
    assert prepared_session.initial_session.successful_run_calls == 1


@pytest.mark.parametrize(
    ("initial_run_kind", "initial_provider_session_id", "observed_provider_session_id"),
    [
        (RunKind.FRESH, "provider-fresh", "provider-fresh-runtime"),
        (RunKind.RESUME, "provider-resume", "provider-resume-runtime"),
    ],
)
def test_work_invocation_typed_success_records_provider_session_metadata_through_prepared_run_session(
    tmp_path: Path,
    initial_run_kind: RunKind,
    initial_provider_session_id: str,
    observed_provider_session_id: str,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=initial_run_kind,
        initial_provider_session_id=initial_provider_session_id,
    )

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, prompt, run_kind, session_uuid
            assert on_provider_session_id is not None
            on_provider_session_id(observed_provider_session_id)
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del run_kind, container_exec
        return "caller-rendered prompt text"

    asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert prepared_session.initial_session.recorded_provider_session_ids == [
        observed_provider_session_id
    ]
    assert prepared_session.initial_session.provider_session_id == (
        observed_provider_session_id
    )
    assert prepared_session.initial_session.successful_run_calls == 1


def test_work_invocation_protocol_reprompt_success_records_metadata_on_successful_reprompt_run(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionWithRepromptStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        reprompt_run_kind=RunKind.RESUME,
        reprompt_provider_session_id="provider-resume",
    )
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role
            work_calls.append((run_kind, session_uuid, prompt))
            if len(work_calls) == 1:
                raise AgentOutputProtocolError("missing tag")
            assert on_provider_session_id is not None
            on_provider_session_id("provider-resume-runtime")
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.FRESH
        return "caller-rendered prompt text"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert work_calls == [
        (RunKind.FRESH, "provider-fresh", "caller-rendered prompt text"),
        (RunKind.RESUME, "provider-resume", "reprompt"),
    ]
    assert prepared_session.initial_session.successful_run_calls == 0
    assert prepared_session.reprompt_session.recorded_provider_session_ids == [
        "provider-resume-runtime"
    ]
    assert prepared_session.reprompt_session.provider_session_id == (
        "provider-resume-runtime"
    )
    assert prepared_session.reprompt_session.successful_run_calls == 1


def test_work_invocation_protocol_reprompt_for_planner_includes_parser_error_and_shape(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionWithRepromptStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        reprompt_run_kind=RunKind.RESUME,
        reprompt_provider_session_id="provider-resume",
    )
    work_calls: list[tuple[RunKind, str | None, str]] = []

    expected_output_shape = (
        Path(__file__).parent.parent
        / "src/pycastle/defaults/prompts/coordination/_expected-output-shape-plan.md"
    ).read_text(encoding="utf-8")
    expected_output_shape = expected_output_shape.replace(
        "{{READY_FOR_AGENT_LABEL}}", "ready-for-agent"
    )
    parser_error = "Planner produced malformed JSON inside <plan> tag: boom"

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            if len(work_calls) == 1:
                raise PlanParseError(parser_error)
            return PlannerOutput(issues=[])

    def reprompt_message(error: str | None) -> str:
        return "\n".join(
            [
                "Your last response did not include the required protocol output.",
                "Please review the task requirements and try again, making sure to include the required output tag.",
                f"The parser reported the following error: {error}",
                "On retry, return a raw JSON object in a `<plan>` tag (do not quote or escape the JSON).",
                expected_output_shape,
            ]
        )

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        return "caller-rendered prompt text"

    asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message=reprompt_message,
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=0,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
            )
        )
    )

    assert len(work_calls) == 2
    assert work_calls[0][0] is RunKind.FRESH
    assert work_calls[1][0] is RunKind.RESUME
    reprompt_prompt = work_calls[1][2]
    assert parser_error in reprompt_prompt
    assert expected_output_shape in reprompt_prompt
    assert "raw JSON object in a `<plan>` tag" in reprompt_prompt


def test_work_invocation_protocol_reprompt_exhaustion_raises_protocol_error_and_closes_failed_status(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionWithRepromptStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        reprompt_run_kind=RunKind.RESUME,
        reprompt_provider_session_id="provider-resume",
    )
    status_display = RecordingStatusDisplay()
    work_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role, on_provider_session_id
            work_calls.append((run_kind, session_uuid, prompt))
            raise AgentOutputProtocolError("missing tag")

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        assert run_kind is RunKind.FRESH
        return "caller-rendered prompt text"

    with pytest.raises(AgentFailedError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=ProtocolOutputAdapter(
                        prompt_factory=prompt_factory,
                        reprompt_message="reprompt",
                    ),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                )
            )
        )

    assert exc_info.value.failure_class == "protocol_error"
    assert work_calls == [
        (RunKind.FRESH, "provider-fresh", "caller-rendered prompt text"),
        (RunKind.RESUME, "provider-resume", "reprompt"),
        (RunKind.RESUME, "provider-resume", "reprompt"),
    ]
    assert ("remove", "Planner", "failed", "error") in status_display.calls


def test_work_invocation_text_output_has_no_protocol_reprompt_path(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionWithRepromptStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        reprompt_run_kind=RunKind.RESUME,
        reprompt_provider_session_id="provider-resume",
    )
    status_display = RecordingStatusDisplay()
    work_text_calls: list[tuple[RunKind, str | None, str]] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del role, tool_policy, on_provider_session_id
            work_text_calls.append((run_kind, session_uuid, prompt))
            raise AgentOutputProtocolError("text outputs do not reprompt")

    with pytest.raises(AgentOutputProtocolError, match="text outputs do not reprompt"):
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Prompt",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=TextOutputAdapter(prompt="text prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: prepared_session,
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                )
            )
        )

    assert work_text_calls == [(RunKind.FRESH, "provider-fresh", "text prompt")]
    assert ("remove", "Prompt", "failed", "error") in status_display.calls


def test_work_invocation_timeout_retry_uses_resumable_provider_run_state_and_successful_retry_metadata(
    tmp_path: Path,
):
    prepared_session = _PreparedRunSessionStandIn(
        initial_run_kind=RunKind.FRESH,
        initial_provider_session_id="provider-fresh",
        resumable_run_kind=RunKind.RESUME,
        resumable_provider_session_id="provider-resume",
    )
    status_display = RecordingStatusDisplay()
    work_calls: list[tuple[RunKind, str | None, str]] = []
    prompt_run_kinds: list[RunKind] = []

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> PlannerOutput:
            del role
            work_calls.append((run_kind, session_uuid, prompt))
            assert on_provider_session_id is not None
            if len(work_calls) == 1:
                on_provider_session_id("provider-fresh-runtime")
                raise AgentTimeoutError("timeout")
            if len(work_calls) == 2:
                on_provider_session_id("provider-resume-runtime-1")
                raise AgentTimeoutError("timeout")
            on_provider_session_id("provider-resume-runtime-2")
            return PlannerOutput(issues=[])

    async def prompt_factory(*, run_kind: RunKind, container_exec) -> str:
        del container_exec
        prompt_run_kinds.append(run_kind)
        return f"{run_kind.value} prompt"

    result = asyncio.run(
        invoke_work(
            WorkInvocationRequest(
                name="Planner",
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                service=ClaudeService(),
                model="sonnet",
                effort="high",
                output_adapter=ProtocolOutputAdapter(
                    prompt_factory=prompt_factory,
                    reprompt_message="reprompt",
                ),
                dependencies=WorkInvocationDependencies(
                    container_workspace="/home/agent/workspace",
                    timeout_retries=2,
                    stage_key_for_role=lambda role: role.value,
                    prepare_session=lambda _request: prepared_session,
                    build_session=lambda *_args: _FakeSession(),
                    build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
                    get_git_identity=lambda: ("Test User", "test@example.com"),
                ),
                status_display=status_display,
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert prompt_run_kinds == [RunKind.FRESH, RunKind.RESUME, RunKind.RESUME]
    assert work_calls == [
        (RunKind.FRESH, "provider-fresh", "fresh prompt"),
        (RunKind.RESUME, "provider-resume", "resume prompt"),
        (RunKind.RESUME, "provider-resume-runtime-1", "resume prompt"),
    ]
    assert prepared_session.initial_session.recorded_provider_session_ids == [
        "provider-fresh-runtime"
    ]
    assert prepared_session.initial_session.successful_run_calls == 0
    assert prepared_session.resumable_session.recorded_provider_session_ids == [
        "provider-resume-runtime-1",
        "provider-resume-runtime-2",
    ]
    assert prepared_session.resumable_session.provider_session_id == (
        "provider-resume-runtime-2"
    )
    assert prepared_session.resumable_session.successful_run_calls == 1
    assert ("print", "Planner", "Timeout — restarting (attempt 1/2)", None) in (
        status_display.calls
    )
    assert ("print", "Planner", "Timeout — restarting (attempt 2/2)", None) in (
        status_display.calls
    )


def test_work_invocation_timeout_exhaustion_preserves_agent_timeout_context(
    tmp_path: Path,
):
    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentTimeoutError("timeout")

    with pytest.raises(AgentTimeoutError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Runtime Consumer",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.IMPLEMENTER,
                    service=ClaudeService(),
                    model="sonnet",
                    effort="high",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=0,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: _PreparedRunSessionStandIn(
                            initial_run_kind=RunKind.FRESH,
                            initial_provider_session_id=None,
                        ),
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                )
            )
        )

    err = exc_info.value
    assert err.role_value == AgentRole.IMPLEMENTER.value
    assert err.worktree_path == _managed_mount(tmp_path)


def test_work_invocation_opencode_idle_timeout_exhaustion_becomes_usage_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import dataclasses
    from datetime import datetime, timedelta
    from pycastle.services._wake_time import compute_wake_time

    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentTimeoutError("timeout")

    class _ExhaustibleOpencodeService:
        name = "opencode"

        def __init__(self) -> None:
            self.mark_exhausted_calls: list[datetime | None] = []

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

    now = datetime(2025, 1, 1, 14, 30, 0)
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)

    expected_reset = compute_wake_time(None, now, timedelta(minutes=90))[0].replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    runner = AgentRunner(
        {},
        _make_cfg(
            tmp_path,
            timeout_retries=2,
            opencode_minimum_unknown_reset_duration_hours=1.5,
        ),
        _make_git_service(),
    )
    service = _ExhaustibleOpencodeService()
    dependencies = runner.build_work_dependencies(
        name="Planner",
        model="deepseek-v4-flash",
        effort="medium",
        service=cast(Any, service),
    )
    dependencies = dataclasses.replace(
        dependencies,
        prepare_session=lambda request: _PreparedRunSessionStandIn(
            initial_run_kind=RunKind.FRESH,
            initial_provider_session_id=None,
        ),
        build_session=lambda *_args: _FakeSession(),
        build_runner=lambda *_args: cast(ContainerRunner, _FakeRunner()),
    )

    status_display = RecordingStatusDisplay()

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=cast(Any, service),
                    model="deepseek-v4-flash",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=dependencies,
                    status_display=status_display,
                )
            )
        )

    err = exc_info.value
    assert err.provider == "opencode"
    assert err.raw_message is None
    assert err.reset_time is None
    assert err.stage_key == "plan"
    assert service.mark_exhausted_calls == [expected_reset]
    assert (
        "print",
        "Planner",
        "Timeout — restarting (attempt 1/2)",
        None,
    ) in status_display.calls
    assert (
        "print",
        "Planner",
        "Timeout — restarting (attempt 2/2)",
        None,
    ) in status_display.calls


@pytest.mark.parametrize("service_name", ["claude", "codex"])
def test_work_invocation_non_opencode_idle_timeout_exhaustion_remains_timeout(
    tmp_path: Path,
    service_name: str,
):
    class _FakeSession:
        def exec_simple(self, cmd: str) -> str:
            raise AssertionError(f"unexpected container exec: {cmd}")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeRunner:
        async def setup(self, git_name: str, git_email: str, work_body: str) -> None:
            del git_name, git_email, work_body

        async def work_text(
            self,
            prompt: str,
            *,
            role: AgentRole,
            tool_policy,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id=None,
        ) -> str:
            del (
                prompt,
                role,
                tool_policy,
                run_kind,
                session_uuid,
                on_provider_session_id,
            )
            raise AgentTimeoutError("timeout")

    class _TimeoutService:
        def __init__(self, name: str) -> None:
            self.name = name
            self.mark_exhausted_calls: list[datetime | None] = []

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

    service = _TimeoutService(service_name)
    status_display = RecordingStatusDisplay()

    with pytest.raises(AgentTimeoutError) as exc_info:
        asyncio.run(
            invoke_work(
                WorkInvocationRequest(
                    name="Planner",
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service=cast(Any, service),
                    model="test-model",
                    effort="medium",
                    output_adapter=TextOutputAdapter(prompt="already-rendered prompt"),
                    dependencies=WorkInvocationDependencies(
                        container_workspace="/home/agent/workspace",
                        timeout_retries=2,
                        stage_key_for_role=lambda role: role.value,
                        prepare_session=lambda _request: _PreparedRunSessionStandIn(
                            initial_run_kind=RunKind.FRESH,
                            initial_provider_session_id=None,
                        ),
                        build_session=lambda *_args: _FakeSession(),
                        build_runner=lambda *_args: cast(
                            ContainerRunner, _FakeRunner()
                        ),
                        get_git_identity=lambda: ("Test User", "test@example.com"),
                    ),
                    status_display=status_display,
                )
            )
        )

    err = exc_info.value
    assert err.role_value == AgentRole.PLANNER.value
    assert err.worktree_path == _managed_mount(tmp_path)
    assert service.mark_exhausted_calls == []
    assert (
        "print",
        "Planner",
        "Timeout — restarting (attempt 1/2)",
        None,
    ) in status_display.calls
    assert (
        "print",
        "Planner",
        "Timeout — restarting (attempt 2/2)",
        None,
    ) in status_display.calls


def test_work_invocation_translates_runtime_usage_limit_to_pycastle_compatibility_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pycastle.agents import _work_invocation as work_invocation_module

    reset_time = datetime(2026, 6, 8, 12, 0, 0)

    async def fake_runtime_invoke_work(request):
        del request
        raise UsageLimitError(
            reset_time=reset_time,
            raw_message="limit",
            provider="codex",
            is_permanent=True,
            account_label="lineage-a",
            stage_key="plan",
        )

    monkeypatch.setattr(
        work_invocation_module,
        "runtime_invoke_work",
        fake_runtime_invoke_work,
    )

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(invoke_work(cast(WorkInvocationRequest[Any], object())))

    assert type(exc_info.value) is UsageLimitError
    assert exc_info.value.reset_time == reset_time
    assert exc_info.value.raw_message == "limit"
    assert exc_info.value.provider == "codex"
    assert exc_info.value.is_permanent is True
    assert exc_info.value.account_label == "lineage-a"
    assert exc_info.value.stage_key == "plan"


def test_work_invocation_preserves_existing_pycastle_usage_limit_error_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pycastle.agents import _work_invocation as work_invocation_module

    error = UsageLimitError(stage_key="plan")

    async def fake_runtime_invoke_work(request):
        del request
        raise error

    monkeypatch.setattr(
        work_invocation_module,
        "runtime_invoke_work",
        fake_runtime_invoke_work,
    )

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(invoke_work(cast(WorkInvocationRequest[Any], object())))

    assert exc_info.value is error


# ── AgentRunner: prompt dispatch delegation ───────────────────────────────────


async def _noop_exec(cmd: str) -> str:
    return ""


def _make_build_prompt_cfg(tmp_path: Path) -> Config:
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "coordination").mkdir(exist_ok=True)
    (prompts_dir / "shared").mkdir(exist_ok=True)
    (prompts_dir / "coordination/plan.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "shared/resume.md").write_text("resume-content", encoding="utf-8")
    return Config(logs_dir=tmp_path)


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
def test_agent_runner_run_delegates_work_prompt_rendering_to_prompt_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}
    dispatch_calls: list[dict[str, object]] = []

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[PlannerOutput],
    ) -> PlannerOutput:
        captured["request"] = request
        return PlannerOutput(issues=[])

    async def fake_render_prompt_invocation(
        invocation,
        *,
        renderer,
        run_kind,
        exec_fn,
    ) -> str:
        if run_kind is RunKind.RESUME and not invocation.send_role_prompt_on_resume:
            rendered_prompt = await renderer.render(PromptTemplate.RESUME, {}, exec_fn)
        else:
            rendered_prompt = await renderer.render(
                invocation.template,
                invocation.scope_args,
                exec_fn,
            )
        dispatch_calls.append(
            {
                "invocation": invocation,
                "renderer": renderer,
                "run_kind": run_kind,
                "exec_fn": exec_fn,
                "rendered_prompt": rendered_prompt,
            }
        )
        return rendered_prompt

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    monkeypatch.setattr(
        "pycastle.agents.runner.render_prompt_invocation",
        fake_render_prompt_invocation,
    )
    runner = AgentRunner({}, _make_build_prompt_cfg(tmp_path), _make_git_service())

    asyncio.run(
        runner.run(
            _run_request(
                name="Planner",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                send_role_prompt_on_resume=False,
            )
        )
    )

    work_request = cast(
        runtime_work.WorkInvocationRequest[PlannerOutput],
        captured["request"],
    )
    result = asyncio.run(
        work_request.output_adapter.build_prompt(
            run_kind=RunKind.RESUME,
            container_exec=_noop_exec,
        )
    )

    assert result == "resume-content"
    assert len(dispatch_calls) == 1
    dispatch_call = dispatch_calls[0]
    invocation = dispatch_call["invocation"]
    assert isinstance(invocation, PromptInvocation)
    assert invocation.template is _PLAN_TEMPLATE
    assert invocation.scope_args == _PLAN_SCOPE_ARGS
    assert invocation.send_role_prompt_on_resume is False
    assert dispatch_call["rendered_prompt"] == result
    assert dispatch_call["run_kind"] is RunKind.RESUME
    assert dispatch_call["exec_fn"] is _noop_exec


def test_agent_runner_run_uses_runtime_client_instead_of_invoke_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pycastle.work as runtime_work

    fake_runtime = _RuntimeSequenceClient(
        [_completed_outcome(output='<plan>{"issues":[]}</plan>', model="sonnet")]
    )

    async def _fail_invoke_work(*_args, **_kwargs):
        raise AssertionError("invoke_work should not be called")

    monkeypatch.setattr(runtime_work, "invoke_work", _fail_invoke_work)
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    _patch_runtime_runner(monkeypatch, runner, fake_runtime)

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Planner",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                model="sonnet",
                effort="high",
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    assert len(fake_runtime.new_session_requests) == 1


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
def test_agent_runner_run_expands_shell_expressions_via_container_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Work prompt builder passes container_exec through to prompt rendering."""
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[PlannerOutput],
    ) -> PlannerOutput:
        captured["request"] = request
        return PlannerOutput(issues=[])

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "shared").mkdir(exist_ok=True)
    (prompts_dir / "shared/resume.md").write_text(
        "Result: !`echo hi`", encoding="utf-8"
    )
    runner = AgentRunner({}, Config(logs_dir=tmp_path), _make_git_service())

    async def fake_exec(cmd: str) -> str:
        if "echo hi" in cmd:
            return "expanded\n"
        return ""

    asyncio.run(
        runner.run(
            _run_request(
                name="Planner",
                template=PromptTemplate.RESUME,
                scope_args={},
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
            ),
        )
    )

    work_request = cast(
        runtime_work.WorkInvocationRequest[PlannerOutput],
        captured["request"],
    )
    result = asyncio.run(
        work_request.output_adapter.build_prompt(
            run_kind=RunKind.RESUME,
            container_exec=fake_exec,
        )
    )

    assert result == "Result: expanded"


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
def test_agent_runner_run_builds_runtime_work_invocation_with_agent_output_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[PlannerOutput],
    ) -> PlannerOutput:
        captured["request"] = request
        return PlannerOutput(issues=[])

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Planner",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
                model="sonnet",
                effort="high",
            )
        )
    )

    assert result == PlannerOutput(issues=[])
    work_request = cast(
        runtime_work.WorkInvocationRequest[PlannerOutput],
        captured["request"],
    )
    assert work_request.name == "Planner"
    assert work_request.mount_path == _managed_mount(tmp_path)
    assert work_request.role is AgentRole.PLANNER
    assert work_request.service.name == "claude"
    assert work_request.model == "sonnet"
    assert work_request.effort == "high"
    assert isinstance(work_request.output_adapter, ProtocolOutputAdapter)
    assert work_request.dependencies.stage_key_for_role(AgentRole.PLANNER) == "plan"
    assert work_request.allow_non_typed_resume_retry is True


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
def test_agent_runner_run_planner_protocol_reprompt_uses_parser_error_and_expected_output_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[PlannerOutput],
    ) -> PlannerOutput:
        captured["request"] = request
        return PlannerOutput(issues=[])

    class _ProtocolErrorRunner:
        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id,
        ) -> PlannerOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            raise PlanParseError(
                "Planner produced malformed JSON inside <plan> tag: boom"
            )

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())

    asyncio.run(
        runner.run(
            _run_request(
                name="Planner",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.PLANNER,
            )
        )
    )

    work_request = cast(
        runtime_work.WorkInvocationRequest[PlannerOutput],
        captured["request"],
    )

    with pytest.raises(PlanParseError, match="malformed JSON inside <plan> tag: boom"):
        asyncio.run(
            work_request.output_adapter.invoke(
                runner=cast(ContainerRunner, _ProtocolErrorRunner()),
                role=AgentRole.PLANNER,
                prompt="unused",
                run_kind=RunKind.FRESH,
                session_uuid=None,
                on_provider_session_id=lambda _provider_session_id: None,
            )
        )

    reprompt = work_request.output_adapter.protocol_reprompt_message()
    assert reprompt is not None
    assert "Planner produced malformed JSON inside <plan> tag: boom" in reprompt
    assert "raw JSON object in a `<plan>` tag" in reprompt
    assert (
        runner._renderer.render_expected_output_shape(_PLAN_TEMPLATE, _PLAN_SCOPE_ARGS)
        in reprompt
    )


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
@pytest.mark.parametrize(
    ("template", "scope_args", "error_text", "expected_snippets"),
    [
        (
            PromptTemplate.IMPROVE_SCAN,
            {"RECENT_IMPROVE_PRD_TITLES": "No recent improve PRDs found."},
            "Agent produced no <promise>COMPLETE</promise> or <promise>NO-CANDIDATE</promise> tag.",
            (
                "<promise>COMPLETE</promise>",
                "<promise>NO-CANDIDATE</promise>",
            ),
        ),
        (
            PromptTemplate.IMPROVE_PRD,
            {
                "IMPROVE_SHORT_SID": "abc123",
                "RECENT_IMPROVE_PRDS": "No recent improve PRDs found.",
            },
            "Malformed JSON inside <issue> tag: boom",
            (
                '<issue>{"number": N, "labels": []}</issue>',
                "<promise>COMPLETE</promise>",
            ),
        ),
        (
            PromptTemplate.IMPROVE_ISSUES,
            {
                "IMPROVE_SHORT_SID": "abc123",
                "ISSUE_NUMBER": "77",
                "ISSUE_TITLE": "Improve PRD",
                "ISSUE_BODY": "PRD body",
                "ISSUE_COMMENTS": "No comments.",
            },
            "Agent produced no <promise>COMPLETE</promise> or <promise>NO-CANDIDATE</promise> tag.",
            (
                "Output each filed issue number as `<issue>N</issue>`.",
                "<promise>COMPLETE</promise>",
            ),
        ),
        (
            PromptTemplate.IMPROVE_NO_CANDIDATE,
            {
                "IMPROVE_SHORT_SID": "abc123",
                "RECENT_IMPROVE_PRDS": "No recent improve PRDs found.",
            },
            "Agent produced no <promise>COMPLETE</promise> or <promise>NO-CANDIDATE</promise> tag.",
            (
                "Output each filed issue number as `<issue>N</issue>`.",
                "<promise>COMPLETE</promise>",
            ),
        ),
    ],
)
def test_agent_runner_run_improve_protocol_reprompt_uses_phase_specific_expected_output_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: PromptTemplate,
    scope_args: dict[str, str],
    error_text: str,
    expected_snippets: tuple[str, str],
) -> None:
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[CompletionOutput | IssueOutput],
    ) -> CompletionOutput:
        captured["request"] = request
        return CompletionOutput()

    class _ProtocolErrorRunner:
        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id,
        ) -> CompletionOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            raise AgentOutputProtocolError(error_text)

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())

    asyncio.run(
        runner.run(
            _run_request(
                name="Improve Agent",
                template=template,
                scope_args=scope_args,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.IMPROVE,
            )
        )
    )

    work_request = cast(
        runtime_work.WorkInvocationRequest[CompletionOutput | IssueOutput],
        captured["request"],
    )

    with pytest.raises(AgentOutputProtocolError, match=error_text):
        asyncio.run(
            work_request.output_adapter.invoke(
                runner=cast(ContainerRunner, _ProtocolErrorRunner()),
                role=AgentRole.IMPROVE,
                prompt="unused",
                run_kind=RunKind.FRESH,
                session_uuid=None,
                on_provider_session_id=lambda _provider_session_id: None,
            )
        )

    reprompt = work_request.output_adapter.protocol_reprompt_message()
    assert reprompt is not None
    assert "Use this Improve output shape exactly:" in reprompt
    assert error_text in reprompt
    for snippet in expected_snippets:
        assert snippet in reprompt
    assert (
        runner._renderer.render_expected_output_shape(template, scope_args) in reprompt
    )


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
@pytest.mark.parametrize(
    ("role", "template", "scope_args", "error_text"),
    [
        (
            AgentRole.PREFLIGHT_ISSUE,
            PromptTemplate.PREFLIGHT_ISSUE,
            {
                "CHECK_NAME": "[PREFLIGHT] test",
                "COMMAND": "pytest -q",
                "OUTPUT": "tests fail",
            },
            "Malformed JSON in <issue> tag: boom",
        ),
        (
            AgentRole.FAILURE_REPORT,
            PromptTemplate.FAILURE_REPORT,
            {
                "FAILED_ROLE": "implementer",
                "SESSION_DIR": "/tmp/session",
                "FAILURE_CLASS": "protocol_error",
                "EVIDENCE_PATH": "",
                "HAS_EVIDENCE_PATH": "no",
            },
            "Malformed JSON in <issue> tag: boom",
        ),
    ],
)
def test_agent_runner_run_issue_template_protocol_reprompt_includes_expected_output_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: AgentRole,
    template: PromptTemplate,
    scope_args: dict[str, str],
    error_text: str,
) -> None:
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[CompletionOutput | IssueOutput],
    ) -> CompletionOutput:
        captured["request"] = request
        return CompletionOutput()

    class _ProtocolErrorRunner:
        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id,
        ) -> CompletionOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            raise AgentOutputProtocolError(error_text)

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())

    asyncio.run(
        runner.run(
            _run_request(
                name="Issue Reporter",
                template=template,
                scope_args=scope_args,
                mount_path=_managed_mount(tmp_path),
                role=role,
            )
        )
    )

    work_request = cast(
        runtime_work.WorkInvocationRequest[CompletionOutput | IssueOutput],
        captured["request"],
    )

    with pytest.raises(AgentOutputProtocolError, match="boom"):
        asyncio.run(
            work_request.output_adapter.invoke(
                runner=cast(ContainerRunner, _ProtocolErrorRunner()),
                role=role,
                prompt="unused",
                run_kind=RunKind.FRESH,
                session_uuid=None,
                on_provider_session_id=lambda _provider_session_id: None,
            )
        )

    reprompt = work_request.output_adapter.protocol_reprompt_message()
    assert reprompt is not None
    assert error_text in reprompt
    assert (
        runner._renderer.render_expected_output_shape(template, scope_args) in reprompt
    )


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
@pytest.mark.parametrize(
    ("role", "template", "scope_args", "error_text"),
    [
        (
            AgentRole.IMPLEMENTER,
            PromptTemplate.IMPLEMENT_DOCS,
            {
                "ISSUE_NUMBER": "77",
                "ISSUE_TITLE": "Doc bug",
                "ISSUE_BODY": "Body",
                "ISSUE_COMMENTS": "",
                "BRANCH": "issue-77-docs",
                "INTERRUPTED_WORK": "",
            },
            "<commit_message> tag is missing",
        ),
        (
            AgentRole.REVIEWER,
            PromptTemplate.REVIEW,
            {
                "ISSUE_NUMBER": "77",
                "ISSUE_TITLE": "Doc bug",
                "ISSUE_BODY": "Body",
                "ISSUE_COMMENTS": "",
                "BRANCH": "issue-77-docs",
                "INTERRUPTED_WORK": "",
            },
            "<commit_message> tag is missing",
        ),
        (
            AgentRole.MERGER,
            PromptTemplate.MERGE,
            {"BRANCHES": "issue-77-docs"},
            "<commit_message> tag is missing",
        ),
    ],
)
def test_agent_runner_run_host_parsed_commit_message_templates_reprompt_with_expected_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: AgentRole,
    template: PromptTemplate,
    scope_args: dict[str, str],
    error_text: str,
) -> None:
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[CompletionOutput],
    ) -> CompletionOutput:
        captured["request"] = request
        return CompletionOutput()

    class _ProtocolErrorRunner:
        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id,
        ) -> CompletionOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            raise AgentOutputProtocolError(error_text)

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())

    asyncio.run(
        runner.run(
            _run_request(
                name="Code Agent",
                template=template,
                scope_args=scope_args,
                mount_path=_managed_mount(tmp_path),
                role=role,
            )
        )
    )

    work_request = cast(
        runtime_work.WorkInvocationRequest[CompletionOutput],
        captured["request"],
    )

    with pytest.raises(AgentOutputProtocolError, match="missing"):
        asyncio.run(
            work_request.output_adapter.invoke(
                runner=cast(ContainerRunner, _ProtocolErrorRunner()),
                role=role,
                prompt="unused",
                run_kind=RunKind.FRESH,
                session_uuid=None,
                on_provider_session_id=lambda _provider_session_id: None,
            )
        )

    reprompt = work_request.output_adapter.protocol_reprompt_message()
    assert reprompt is not None
    assert error_text in reprompt
    assert "Use this output shape exactly:" in reprompt
    assert (
        runner._renderer.render_expected_output_shape(template, scope_args) in reprompt
    )


@pytest.mark.xfail(
    reason="invoke_work seam removed; runtime-client prompt coverage lives in newer tests",
    strict=False,
)
def test_agent_runner_run_divergence_resolver_protocol_reprompt_includes_expected_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pycastle.work as runtime_work

    captured: dict[str, object] = {}
    scope_args = {"BRANCH": "issue-77-docs"}
    error_text = "Agent returned no <promise> tags"

    async def fake_invoke_work(
        request: runtime_work.WorkInvocationRequest[CompletionOutput],
    ) -> CompletionOutput:
        captured["request"] = request
        return CompletionOutput()

    class _ProtocolErrorRunner:
        async def work(
            self,
            role: AgentRole,
            prompt: str,
            *,
            run_kind: RunKind,
            session_uuid: str | None,
            on_provider_session_id,
        ) -> CompletionOutput:
            del role, prompt, run_kind, session_uuid, on_provider_session_id
            raise AgentOutputProtocolError(error_text)

    monkeypatch.setattr(runtime_work, "invoke_work", fake_invoke_work)
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())

    asyncio.run(
        runner.run(
            _run_request(
                name="Resolver",
                template=PromptTemplate.DIVERGENCE_RESOLVE,
                scope_args=scope_args,
                mount_path=_managed_mount(tmp_path),
                role=AgentRole.DIVERGENCE_RESOLVER,
            )
        )
    )

    work_request = cast(
        runtime_work.WorkInvocationRequest[CompletionOutput],
        captured["request"],
    )

    with pytest.raises(AgentOutputProtocolError, match="tags"):
        asyncio.run(
            work_request.output_adapter.invoke(
                runner=cast(ContainerRunner, _ProtocolErrorRunner()),
                role=AgentRole.DIVERGENCE_RESOLVER,
                prompt="unused",
                run_kind=RunKind.FRESH,
                session_uuid=None,
                on_provider_session_id=lambda _provider_session_id: None,
            )
        )

    reprompt = work_request.output_adapter.protocol_reprompt_message()
    assert reprompt is not None
    assert error_text in reprompt
    assert "<promise>COMPLETE</promise>" in reprompt
    assert (
        runner._renderer.render_expected_output_shape(
            PromptTemplate.DIVERGENCE_RESOLVE, scope_args
        )
        in reprompt
    )


def test_build_work_dependencies_marks_generic_permanent_service_exhaustion(
    tmp_path: Path,
) -> None:
    class _PermanentExhaustionService:
        name = "fake"

        def __init__(self) -> None:
            self.mark_exhausted_calls: list[datetime | None] = []
            self.mark_permanently_exhausted_calls = 0

        def build_command(
            self, role, model, effort, run_kind, session_uuid, *, tool_policy=None
        ) -> str:
            del role, model, effort, run_kind, session_uuid, tool_policy
            return ""

        def build_env(
            self, state_dir_container_path=None, token=None
        ) -> dict[str, str]:
            del state_dir_container_path, token
            return {}

        def run(self, lines, on_provider_session_id=None):
            del lines, on_provider_session_id
            return iter(())

        def is_available(self, now: datetime | None = None) -> bool:
            del now
            return True

        def next_wake_time(self) -> datetime:
            raise AssertionError("next_wake_time should not be called")

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

        def mark_permanently_exhausted(self) -> str | None:
            self.mark_permanently_exhausted_calls += 1
            return "provider-account"

        def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
            del role, namespace
            return None

        def is_resumable(self, state_dir: Path) -> bool:
            del state_dir
            return False

        def provider_session_preferences(
            self,
            request: ProviderSessionPreferencesRequest,
        ) -> ProviderSessionPreferences:
            del request
            return ProviderSessionPreferences()

        def provider_session_state(self, request: ProviderSessionStateRequest):
            del request
            return ProviderSessionState(RunKind.FRESH, None)

        def valid_models(self) -> frozenset[str]:
            return frozenset({"model"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    service = _PermanentExhaustionService()
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())

    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="model",
        effort="medium",
        service=cast(Any, service),
    )
    error = UsageLimitError(reset_time=None, is_permanent=True)

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label == "provider-account"
    assert service.mark_permanently_exhausted_calls == 1
    assert service.mark_exhausted_calls == []


def test_build_work_dependencies_falls_back_to_resettable_service_exhaustion(
    tmp_path: Path,
) -> None:
    class _ResettableService:
        name = "fake"

        def __init__(self) -> None:
            self.mark_exhausted_calls: list[datetime | None] = []

        def build_command(
            self, role, model, effort, run_kind, session_uuid, *, tool_policy=None
        ) -> str:
            del role, model, effort, run_kind, session_uuid, tool_policy
            return ""

        def build_env(
            self, state_dir_container_path=None, token=None
        ) -> dict[str, str]:
            del state_dir_container_path, token
            return {}

        def run(self, lines, on_provider_session_id=None):
            del lines, on_provider_session_id
            return iter(())

        def is_available(self, now: datetime | None = None) -> bool:
            del now
            return True

        def next_wake_time(self) -> datetime:
            raise AssertionError("next_wake_time should not be called")

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

        def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
            del role, namespace
            return None

        def is_resumable(self, state_dir: Path) -> bool:
            del state_dir
            return False

        def provider_session_preferences(
            self,
            request: ProviderSessionPreferencesRequest,
        ) -> ProviderSessionPreferences:
            del request
            return ProviderSessionPreferences()

        def provider_session_state(self, request: ProviderSessionStateRequest):
            del request
            return ProviderSessionState(RunKind.FRESH, None)

        def valid_models(self) -> frozenset[str]:
            return frozenset({"model"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    service = _ResettableService()
    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    reset_time = datetime(2025, 1, 1, 12, 0, 0)

    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="model",
        effort="medium",
        service=cast(Any, service),
    )
    error = UsageLimitError(reset_time=reset_time, is_permanent=True)

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label is None
    assert service.mark_exhausted_calls == [reset_time]


def test_build_work_dependencies_estimates_unknown_reset_for_provider_minimum_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ResettableService:
        name = "codex"

        def __init__(self) -> None:
            self.mark_exhausted_calls: list[datetime | None] = []

        def build_command(
            self, role, model, effort, run_kind, session_uuid, *, tool_policy=None
        ) -> str:
            del role, model, effort, run_kind, session_uuid, tool_policy
            return ""

        def build_env(
            self, state_dir_container_path=None, token=None
        ) -> dict[str, str]:
            del state_dir_container_path, token
            return {}

        def run(self, lines, on_provider_session_id=None):
            del lines, on_provider_session_id
            return iter(())

        def is_available(self, now: datetime | None = None) -> bool:
            del now
            return True

        def next_wake_time(self) -> datetime:
            raise AssertionError("next_wake_time should not be called")

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

        def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
            del role, namespace
            return None

        def is_resumable(self, state_dir: Path) -> bool:
            del state_dir
            return False

        def provider_session_preferences(
            self,
            request: ProviderSessionPreferencesRequest,
        ) -> ProviderSessionPreferences:
            del request
            return ProviderSessionPreferences()

        def provider_session_state(self, request: ProviderSessionStateRequest):
            del request
            return ProviderSessionState(RunKind.FRESH, None)

        def valid_models(self) -> frozenset[str]:
            return frozenset({"model"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    now = datetime(2025, 1, 1, 14, 30, 0)
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)
    service = _ResettableService()
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, codex_minimum_unknown_reset_duration_hours=1.5),
        _make_git_service(),
    )

    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="model",
        effort="medium",
        service=cast(Any, service),
    )
    error = UsageLimitError(reset_time=None, provider="codex")

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label is None
    assert service.mark_exhausted_calls == [datetime(2025, 1, 1, 16, 0, 0)]


def test_build_work_dependencies_keeps_parsed_reset_time_authoritative_with_provider_minimum_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ResettableService:
        name = "codex"

        def __init__(self) -> None:
            self.mark_exhausted_calls: list[datetime | None] = []

        def build_command(
            self, role, model, effort, run_kind, session_uuid, *, tool_policy=None
        ) -> str:
            del role, model, effort, run_kind, session_uuid, tool_policy
            return ""

        def build_env(
            self, state_dir_container_path=None, token=None
        ) -> dict[str, str]:
            del state_dir_container_path, token
            return {}

        def run(self, lines, on_provider_session_id=None):
            del lines, on_provider_session_id
            return iter(())

        def is_available(self, now: datetime | None = None) -> bool:
            del now
            return True

        def next_wake_time(self) -> datetime:
            raise AssertionError("next_wake_time should not be called")

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

        def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
            del role, namespace
            return None

        def is_resumable(self, state_dir: Path) -> bool:
            del state_dir
            return False

        def provider_session_preferences(
            self,
            request: ProviderSessionPreferencesRequest,
        ) -> ProviderSessionPreferences:
            del request
            return ProviderSessionPreferences()

        def provider_session_state(self, request: ProviderSessionStateRequest):
            del request
            return ProviderSessionState(RunKind.FRESH, None)

        def valid_models(self) -> frozenset[str]:
            return frozenset({"model"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    now = datetime(2025, 1, 1, 14, 30, 0)
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)
    service = _ResettableService()
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, codex_minimum_unknown_reset_duration_hours=6),
        _make_git_service(),
    )
    reset_time = datetime(2025, 1, 1, 15, 45, 0)

    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="model",
        effort="medium",
        service=cast(Any, service),
    )
    error = UsageLimitError(reset_time=reset_time, provider="codex")

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label is None
    assert service.mark_exhausted_calls == [reset_time]


def test_build_work_dependencies_uses_opencode_minimum_unknown_reset_duration_for_unknown_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ResettableService:
        name = "opencode"

        def __init__(self) -> None:
            self.mark_exhausted_calls: list[datetime | None] = []

        def build_command(
            self, role, model, effort, run_kind, session_uuid, *, tool_policy=None
        ) -> str:
            del role, model, effort, run_kind, session_uuid, tool_policy
            return ""

        def build_env(
            self, state_dir_container_path=None, token=None
        ) -> dict[str, str]:
            del state_dir_container_path, token
            return {}

        def run(self, lines, on_provider_session_id=None):
            del lines, on_provider_session_id
            return iter(())

        def is_available(self, now: datetime | None = None) -> bool:
            del now
            return True

        def next_wake_time(self) -> datetime:
            raise AssertionError("next_wake_time should not be called")

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

        def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
            del role, namespace
            return None

        def is_resumable(self, state_dir: Path) -> bool:
            del state_dir
            return False

        def provider_session_preferences(
            self,
            request: ProviderSessionPreferencesRequest,
        ) -> ProviderSessionPreferences:
            del request
            return ProviderSessionPreferences()

        def provider_session_state(self, request: ProviderSessionStateRequest):
            del request
            return ProviderSessionState(RunKind.FRESH, None)

        def valid_models(self) -> frozenset[str]:
            return frozenset({"deepseek-v4-flash"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    now = datetime(2025, 1, 1, 14, 30, 0)
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)
    service = _ResettableService()
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, opencode_minimum_unknown_reset_duration_hours=1.5),
        _make_git_service(),
    )

    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="deepseek-v4-flash",
        effort="medium",
        service=cast(Any, service),
    )
    error = UsageLimitError(reset_time=None, provider="opencode")

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label is None
    assert service.mark_exhausted_calls == [datetime(2025, 1, 1, 16, 0, 0)]


def test_build_work_dependencies_keeps_parsed_reset_time_authoritative_for_opencode_with_provider_minimum_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ResettableService:
        name = "opencode"

        def __init__(self) -> None:
            self.mark_exhausted_calls: list[datetime | None] = []

        def build_command(
            self, role, model, effort, run_kind, session_uuid, *, tool_policy=None
        ) -> str:
            del role, model, effort, run_kind, session_uuid, tool_policy
            return ""

        def build_env(
            self, state_dir_container_path=None, token=None
        ) -> dict[str, str]:
            del state_dir_container_path, token
            return {}

        def run(self, lines, on_provider_session_id=None):
            del lines, on_provider_session_id
            return iter(())

        def is_available(self, now: datetime | None = None) -> bool:
            del now
            return True

        def next_wake_time(self) -> datetime:
            raise AssertionError("next_wake_time should not be called")

        def mark_exhausted(self, reset_time: datetime | None) -> None:
            self.mark_exhausted_calls.append(reset_time)

        def state_dir_relpath(self, role: AgentRole, namespace: str = "") -> str | None:
            del role, namespace
            return None

        def is_resumable(self, state_dir: Path) -> bool:
            del state_dir
            return False

        def provider_session_preferences(
            self,
            request: ProviderSessionPreferencesRequest,
        ) -> ProviderSessionPreferences:
            del request
            return ProviderSessionPreferences()

        def provider_session_state(self, request: ProviderSessionStateRequest):
            del request
            return ProviderSessionState(RunKind.FRESH, None)

        def valid_models(self) -> frozenset[str]:
            return frozenset({"deepseek-v4-flash"})

        def valid_efforts(self) -> frozenset[str]:
            return frozenset({"medium"})

    now = datetime(2025, 1, 1, 14, 30, 0)
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)
    service = _ResettableService()
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, opencode_minimum_unknown_reset_duration_hours=6),
        _make_git_service(),
    )
    reset_time = datetime(2025, 1, 1, 15, 45, 0)

    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="deepseek-v4-flash",
        effort="medium",
        service=cast(Any, service),
    )
    error = UsageLimitError(reset_time=reset_time, provider="opencode")

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label is None
    assert service.mark_exhausted_calls == [reset_time]


def test_build_work_dependencies_uses_claude_minimum_unknown_reset_duration_for_unknown_reset_with_account_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2025, 1, 1, 14, 30, 0)
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)
    service = ClaudeService(
        accounts=[("primary", "tok-primary"), ("secondary", "tok-secondary")]
    )
    service.build_env()

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, claude_minimum_unknown_reset_duration_hours=1.5),
        _make_git_service(),
    )
    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="sonnet",
        effort="medium",
        service=service,
    )
    error = UsageLimitError(reset_time=None, provider="claude")

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label is None
    assert service.next_wake_time() == datetime(2025, 1, 1, 16, 2, 0)


def test_build_work_dependencies_keeps_parsed_reset_time_authoritative_for_claude_with_account_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2025, 1, 1, 14, 30, 0)
    monkeypatch.setattr("pycastle.agents.runner._time_module.now_local", lambda: now)
    service = ClaudeService(
        accounts=[("primary", "tok-primary"), ("secondary", "tok-secondary")]
    )
    service.build_env()
    reset_time = datetime(2025, 1, 1, 15, 45, 0)

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path, claude_minimum_unknown_reset_duration_hours=6),
        _make_git_service(),
    )
    dependencies = runner.build_work_dependencies(
        name="Reviewer",
        model="sonnet",
        effort="medium",
        service=service,
    )
    error = UsageLimitError(reset_time=reset_time, provider="claude")

    dependencies.handle_provider_account_exhaustion(service, error)

    assert error.account_label is None
    assert service.next_wake_time() == datetime(2025, 1, 1, 15, 47, 0)


def test_agent_runner_run_prompt_passes_pycastle_adapter_contract_to_runtime_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pycastle.runtime as prompt_runtime
    from pycastle.config.types import StageOverride
    from pycastle.runtime import PromptRunRequest
    from pycastle.services.agent_service import ToolPolicy
    from pycastle.services.service_registry import ServiceRegistry

    codex = CodexService()
    captured: dict[str, object] = {}
    run_session_plan = RunSessionPlan.for_service(
        role=AgentRole.IMPLEMENTER,
        worktree=tmp_path,
        namespace="issues",
        service=codex,
    )

    async def fake_run_prompt(
        *,
        runner,
        service_registry,
        request,
    ) -> str:
        captured["runner"] = runner
        captured["service_registry"] = service_registry
        captured["request"] = request
        return "runtime result"

    monkeypatch.setattr(prompt_runtime, "run_prompt", fake_run_prompt)
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        service_registry={"codex": codex},
    )

    result = asyncio.run(
        runner.run_prompt(
            name="Runtime Consumer",
            prompt="Return the final answer only.",
            mount_path=_managed_mount(tmp_path),
            model="gpt-5.4",
            effort="medium",
            service="codex",
            tool_policy=AgentToolPolicyGroup.PARTIAL,
            session_namespace="issues",
            run_session_plan=run_session_plan,
        )
    )

    assert result == "runtime result"
    assert captured["runner"] is runner
    registry = cast(ServiceRegistry, captured["service_registry"])
    assert isinstance(registry, ServiceRegistry)
    assert registry["codex"] is codex
    prompt_request = cast(PromptRunRequest, captured["request"])
    assert prompt_request.name == "Runtime Consumer"
    assert prompt_request.prompt == "Return the final answer only."
    assert prompt_request.mount_path == _managed_mount(tmp_path)
    assert prompt_request.override == StageOverride(
        service="codex",
        model="gpt-5.4",
        effort="medium",
    )
    assert prompt_request.tool_policy is ToolPolicy.PARTIAL
    assert prompt_request.session_namespace == "issues"
    assert prompt_request.run_session_plan == run_session_plan


# ── HardAgentError: runner cancels token and does NOT mark_exhausted ─────────


def test_agent_runner_cancels_token_on_hard_agent_error(tmp_path):
    """HardAgentError from a 4xx result cancels the CancellationToken and re-raises."""
    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":401,'
            b'"result":"API Error: 401 Unauthorized"}\n'
        ]
    )
    token = CancellationToken()
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(HardAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Implement Agent #42",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                    token=token,
                )
            )
        )

    assert token.is_cancelled


def test_agent_runner_does_not_call_mark_exhausted_on_hard_agent_error(tmp_path):
    """HardAgentError must NOT mark the account exhausted (request-specific, not account-specific)."""
    from pycastle.services.claude_service import ClaudeService

    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":400,'
            b'"result":"API Error: 400 Bad Request"}\n'
        ]
    )
    svc = ClaudeService(accounts=[("primary", "tok-primary")])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(HardAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=_managed_mount(tmp_path),
                )
            )
        )

    # Account must still be available — mark_exhausted was NOT called
    assert svc.is_available() is True


def test_translate_run_outcome_translates_runtime_timeout_to_pycastle_compatibility_error(
    tmp_path: Path,
) -> None:
    from pycastle.agents.runner import translate_run_outcome

    async def fail() -> PlannerOutput:
        raise AgentTimeoutError("timeout")

    with pytest.raises(AgentTimeoutError) as exc_info:
        asyncio.run(
            translate_run_outcome(
                fail(),
                _run_request(
                    name="Planner",
                    template=_PLAN_TEMPLATE,
                    mount_path=_managed_mount(tmp_path),
                    role=AgentRole.PLANNER,
                    service="codex",
                ),
            )
        )

    assert type(exc_info.value) is AgentTimeoutError
    assert exc_info.value.role_value == AgentRole.PLANNER.value
    assert exc_info.value.worktree_path == _managed_mount(tmp_path)


def _make_setup_docker_client() -> MagicMock:
    """Mock docker client that handles container start and non-streaming setup calls."""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=(b"", b""))
    return mock_client
