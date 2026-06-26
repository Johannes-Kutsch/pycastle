import asyncio
import json
from pathlib import Path
from typing import cast

import agent_runtime
import pytest
from agent_runtime.contracts import ToolAccess, ToolPolicyProfile
from agent_runtime.errors import ProviderUnavailableReason
from agent_runtime.runtime import (
    Completed,
    Continuation,
    NewSessionRunRequest,
    ProviderUnavailable,
    ResumedSessionRunRequest,
    RuntimeOutcome,
    RunResult,
    TimedOut,
    UsageLimited,
)
from agent_runtime.types import ProviderSelection, ResolvedProvider

from pycastle.agents.output_protocol import AgentRole, CommitMessageOutput
from pycastle.config import Config, load_config
from pycastle.errors import (
    AgentTimeoutError,
    DockerError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.infrastructure.container_runner import ContainerRunner
from pycastle.infrastructure.docker_session import DockerSession
from pycastle.runtime_session import RunKind
from pycastle.services.runtime_services import AgentService, ToolPolicy
from pycastle.infrastructure.preflight_failure_interpreter import (
    PreflightCommandFailure,
)

from tests.support import RecordingStatusDisplay

_ROLE = AgentRole.IMPLEMENTER


def _make_runtime_complete_outcome(
    text: str,
    continuation: Continuation | None = None,
    *,
    provider: str = "claude",
    model: str = "gpt-5.5",
    effort: str = "medium",
) -> RuntimeOutcome:
    return RuntimeOutcome(
        kind=Completed(),
        result=RunResult(
            output=text,
            usage=None,
            continuation=continuation,
            selected=ResolvedProvider(
                service=provider,
                model=model,
                effort=effort,
            ),
        ),
    )


def _make_runtime_usage_limited_outcome() -> RuntimeOutcome:
    return RuntimeOutcome(
        kind=UsageLimited(reset_time=None),
        result=RunResult(
            output="",
            usage=None,
            continuation=None,
            selected=ResolvedProvider(
                service="claude", model="gpt-5.5", effort="medium"
            ),
        ),
    )


def _make_runtime_unavailable_outcome(
    reason: ProviderUnavailableReason,
) -> RuntimeOutcome:
    return RuntimeOutcome(
        kind=ProviderUnavailable(reason=reason, detail="provider error"),
        result=RunResult(
            output="",
            usage=None,
            continuation=None,
            selected=ResolvedProvider(
                service="claude", model="gpt-5.5", effort="medium"
            ),
        ),
    )


def _make_runtime_timeout_outcome() -> RuntimeOutcome:
    return RuntimeOutcome(
        kind=TimedOut(),
        result=RunResult(
            output="",
            usage=None,
            continuation=None,
            selected=ResolvedProvider(
                service="claude", model="gpt-5.5", effort="medium"
            ),
        ),
    )


class FakeDockerSession:
    def __init__(self, exec_handlers: dict[str, object] | None = None) -> None:
        self.entered = False
        self.exec_calls: list[str] = []
        self._container: object | None = None
        self._exec_handlers = exec_handlers or {}

    def __enter__(self) -> "FakeDockerSession":
        self.entered = True
        return self

    def __exit__(self, *_) -> None:
        pass

    def exec_simple(self, command: str, timeout: float | None = None) -> str:
        del timeout
        self.exec_calls.append(command)
        for needle, handler in self._exec_handlers.items():
            if needle in command:
                if isinstance(handler, BaseException):
                    raise handler
                if callable(handler):
                    return handler(command)
                return str(handler)
        return ""


class FakeRuntimeClient:
    def __init__(self, outcomes: list[RuntimeOutcome] | None = None) -> None:
        self.outcomes = outcomes or []
        self.new_session_requests: list[NewSessionRunRequest] = []
        self.resumed_session_requests: list[ResumedSessionRunRequest] = []

    async def _next_outcome(self) -> RuntimeOutcome:
        if not self.outcomes:
            return _make_runtime_complete_outcome(
                "<commit_message>done</commit_message>"
            )
        return self.outcomes.pop(0)

    async def run_new_session(self, request: NewSessionRunRequest) -> RuntimeOutcome:
        self.new_session_requests.append(request)
        return await self._next_outcome()

    async def run_resumed_session(
        self, request: ResumedSessionRunRequest
    ) -> RuntimeOutcome:
        self.resumed_session_requests.append(request)
        return await self._next_outcome()


class _FakeService:
    def __init__(self, name: str) -> None:
        self.name = name


def _make_runner(
    *,
    name: str = "agent",
    session: FakeDockerSession | None = None,
    status_display=None,
    cfg: Config | None = None,
    tmp_path: Path | None = None,
    model: str = "gpt-5.5",
    effort: str = "medium",
    runtime_client: FakeRuntimeClient | None = None,
    active_container: bool = False,
    mount_path: Path | None = None,
) -> tuple[ContainerRunner, FakeDockerSession]:
    if session is None:
        session = FakeDockerSession()
    if cfg is None:
        cfg = Config(logs_dir=tmp_path or Path("/tmp/pycastle-tests"))
    if active_container:
        session._container = type("Container", (), {"id": "container-123"})()
    runner = ContainerRunner(
        name,
        cast(DockerSession, session),
        model=model,
        effort=effort,
        status_display=status_display,
        cfg=cfg,
        service=cast(AgentService, _FakeService("claude")),
        runtime_client=runtime_client,
        mount_path=mount_path,
    )
    return runner, session


# ── Constructor / runner surface ───────────────────────────────────────────────


def test_container_runner_constructor_takes_session(tmp_path):
    session = FakeDockerSession()
    runner = ContainerRunner(
        "agent",
        cast(DockerSession, session),
        cfg=Config(logs_dir=tmp_path),
        service=_FakeService("claude"),
    )
    assert runner.name == "agent"
    assert runner.log_path.parent == tmp_path


def test_container_runner_does_not_expose_exec_simple_or_write_file(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    assert not hasattr(runner, "exec_simple")
    assert not hasattr(runner, "write_file")


def test_container_runner_builds_argv_transform_for_container_invocation(tmp_path):
    session = FakeDockerSession()
    session._container = type("Container", (), {"id": "container-123"})()
    runner, _ = _make_runner(tmp_path=tmp_path, session=session, active_container=True)

    transform = runner.provider_argv_transform()
    transformed = transform(
        ("claude", "ask"),
        Path("/home/agent/workspace"),
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "token-abc",
            "OPENCODE_CONFIG_CONTENT": "open-code-config",
            "UNRELATED": "ignore-me",
        },
    )

    assert transformed == (
        "docker",
        "exec",
        "container-123",
        "-e",
        "CLAUDE_CODE_OAUTH_TOKEN=token-abc",
        "-e",
        "OPENCODE_CONFIG_CONTENT=open-code-config",
        "claude",
        "ask",
    )


def test_container_runner_argv_transform_raises_without_active_container(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    with pytest.raises(RuntimeError, match="requires an active container"):
        runner.provider_argv_transform()(
            ("claude", "ask"),
            Path("/home/agent/workspace"),
            {"GH_TOKEN": "ghp_1234"},
        )


# ── work() / work_text() against runtime client ───────────────────────────────


def test_work_builds_new_session_runtime_request_with_tool_access_and_argv_transform(
    tmp_path,
):
    runtime = FakeRuntimeClient(
        [_make_runtime_complete_outcome("<commit_message>done</commit_message>")]
    )
    mount = tmp_path / "mount"
    mount.mkdir()
    runner, _ = _make_runner(
        tmp_path=tmp_path,
        model="gpt-4",
        effort="high",
        runtime_client=runtime,
        active_container=True,
        mount_path=mount,
    )

    result = asyncio.run(runner.work(_ROLE, "implement this change"))

    assert isinstance(result, CommitMessageOutput)
    assert len(runtime.new_session_requests) == 1
    request = runtime.new_session_requests[0]
    assert request.provider_selection == ProviderSelection(
        service="claude", model="gpt-4", effort="high"
    )
    assert request.invocation_dir == mount
    assert request.session_store == mount
    assert request.tool_access.kind == "workspace_backed"
    assert request.tool_access.workspace == mount
    transformed = request.argv_transform(
        ("claude", "ask"),
        Path("/tmp"),
        {"OPENCODE_CONFIG_CONTENT": "cfg"},
    )
    assert transformed[0:3] == ("docker", "exec", "container-123")
    assert "-e" in transformed
    assert "OPENCODE_CONFIG_CONTENT=cfg" in transformed
    assert "claude" in transformed


def test_work_invocation_dir_is_a_valid_host_path_not_container_workspace(tmp_path):
    # invocation_dir is forwarded to agent_runtime as cwd for subprocess.Popen on
    # the HOST.  The docker argv_transform wraps the command with `docker exec <id>`
    # and discards invocation_dir for the in-container working directory, but
    # Python's subprocess.Popen validates cwd existence on the host BEFORE forking.
    # Hardcoding the container-internal path /home/agent/workspace therefore causes
    # FileNotFoundError on any host that hasn't mounted that path locally.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runtime = FakeRuntimeClient(
        [_make_runtime_complete_outcome("<commit_message>done</commit_message>")]
    )
    runner, _ = _make_runner(
        tmp_path=tmp_path,
        runtime_client=runtime,
        active_container=True,
        mount_path=worktree,
    )

    asyncio.run(runner.work(_ROLE, "implement this change"))

    request = runtime.new_session_requests[0]
    assert request.invocation_dir == worktree, (
        "invocation_dir must be the host-side worktree path; "
        "/home/agent/workspace only exists inside the Docker container"
    )


def test_work_builds_resumed_session_runtime_request_with_continuation_state(tmp_path):
    runtime = FakeRuntimeClient(
        [_make_runtime_complete_outcome("<commit_message>done</commit_message>")]
    )
    mount = tmp_path / "mount"
    mount.mkdir()
    runner, _ = _make_runner(
        tmp_path=tmp_path,
        model="gpt-4",
        effort="high",
        runtime_client=runtime,
        active_container=True,
        mount_path=mount,
    )

    asyncio.run(
        runner.work(_ROLE, "resume", run_kind=RunKind.RESUME, session_uuid="prov-1")
    )

    request = runtime.resumed_session_requests[0]
    assert request.model == "gpt-4"
    assert request.effort == "high"
    assert request.invocation_dir == mount
    assert request.session_store == mount
    assert request.continuation is not None
    assert request.continuation.provider_resume_state == {
        "provider_session_id": "prov-1"
    }


def test_work_records_provider_session_id_from_continuation(tmp_path):
    continuation = Continuation(
        selected_service="claude",
        selected_model="gpt-5.5",
        selected_effort="medium",
        tool_access=ToolAccess(
            kind="none",
            workspace=None,
            tool_policy=agent_runtime.contracts.ToolPolicy.NONE,
        ),
        provider_resume_state={},
        serialized="cont-1",
    )
    runtime = FakeRuntimeClient(
        [
            _make_runtime_complete_outcome(
                "<commit_message>done</commit_message>",
                continuation=continuation,
            )
        ]
    )
    runner, _ = _make_runner(
        tmp_path=tmp_path, runtime_client=runtime, active_container=True
    )

    received: list[str] = []
    result = asyncio.run(
        runner.work(_ROLE, "prompt", on_provider_session_id=received.append)
    )

    assert isinstance(result, CommitMessageOutput)
    assert received == ["cont-1"]
    header = json.loads(runner.log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["provider_session_id"] == "cont-1"


def test_work_text_maps_tool_policy_to_runtime_tool_access(tmp_path):
    runtime = FakeRuntimeClient([_make_runtime_complete_outcome("plain output")])
    runner, _ = _make_runner(
        tmp_path=tmp_path,
        runtime_client=runtime,
        active_container=True,
    )
    text = asyncio.run(runner.work_text("prompt", tool_policy=ToolPolicy.PARTIAL))

    assert text == "plain output"
    policy = runtime.new_session_requests[0].tool_access.tool_policy
    assert isinstance(policy, ToolPolicyProfile)
    assert "Edit" in policy.disallowed_tools


def test_work_text_records_tokens_when_output_has_usage(tmp_path):
    usage = agent_runtime.runtime.ProviderUsage(
        input_tokens=12_000, cache_creation_input_tokens=4_000
    )
    runtime = FakeRuntimeClient(
        [
            RuntimeOutcome(
                kind=Completed(),
                result=RunResult(
                    output="plain text",
                    usage=usage,
                    continuation=None,
                    selected=ResolvedProvider(
                        service="claude", model="gpt-5.5", effort="medium"
                    ),
                ),
            )
        ]
    )
    display = RecordingStatusDisplay()
    runner, _ = _make_runner(
        tmp_path=tmp_path,
        runtime_client=runtime,
        status_display=display,
        active_container=True,
    )

    text = asyncio.run(runner.work_text("prompt"))

    assert text == "plain text"
    assert ("update_tokens", "agent", 16_000) in display.calls


def test_work_propagates_usage_limit_error(tmp_path):
    runtime = FakeRuntimeClient([_make_runtime_usage_limited_outcome()])
    runner, _ = _make_runner(
        tmp_path=tmp_path, runtime_client=runtime, active_container=True
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(runner.work(_ROLE, "prompt"))


def test_work_propagates_transient_provider_error_as_transient_agent_error(tmp_path):
    runtime = FakeRuntimeClient(
        [
            _make_runtime_unavailable_outcome(
                ProviderUnavailableReason.TRANSIENT_API_ERROR
            )
        ]
    )
    runner, _ = _make_runner(
        tmp_path=tmp_path, runtime_client=runtime, active_container=True
    )

    with pytest.raises(TransientAgentError):
        asyncio.run(runner.work(_ROLE, "prompt"))


def test_work_propagates_timeout_error(tmp_path):
    runtime = FakeRuntimeClient([_make_runtime_timeout_outcome()])
    runner, _ = _make_runner(
        tmp_path=tmp_path, runtime_client=runtime, active_container=True
    )

    with pytest.raises(AgentTimeoutError):
        asyncio.run(runner.work(_ROLE, "prompt"))


def test_work_reuses_single_log_file_for_multiple_invocations(tmp_path):
    runtime = FakeRuntimeClient(
        [
            _make_runtime_complete_outcome("<commit_message>one</commit_message>"),
            _make_runtime_complete_outcome("<commit_message>two</commit_message>"),
        ]
    )
    runner, _ = _make_runner(
        tmp_path=tmp_path, runtime_client=runtime, active_container=True
    )

    asyncio.run(runner.work(_ROLE, "First prompt"))
    asyncio.run(runner.work(_ROLE, "Second prompt"))

    log_text = runner.log_path.read_text(encoding="utf-8")
    assert "First prompt" in log_text
    assert "Second prompt" in log_text


def test_container_runners_keep_logical_sessions_separate(tmp_path):
    runner_a, _ = _make_runner(name="agent-a", tmp_path=tmp_path, active_container=True)
    runner_b, _ = _make_runner(name="agent-b", tmp_path=tmp_path, active_container=True)

    assert runner_a.log_path != runner_b.log_path


# ── setup() ──────────────────────────────────────────────────────────────────


def test_setup_enters_session_and_bootstraps_environment(tmp_path):
    runner, session = _make_runner(tmp_path=tmp_path)
    asyncio.run(runner.setup("Alice", "alice@example.com"))

    assert session.entered
    assert any(
        "git config --global user.name" in c and "Alice" in c
        for c in session.exec_calls
    )
    assert any(
        "git config --global user.email" in c and "alice@example.com" in c
        for c in session.exec_calls
    )
    assert any("pip install" in c for c in session.exec_calls)


def test_setup_propagates_docker_error_when_pip_install_fails(tmp_path):
    session = FakeDockerSession(
        exec_handlers={"pip install": DockerError("pip install failed")}
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)

    with pytest.raises(DockerError, match="pip install failed"):
        asyncio.run(runner.setup("Alice", "alice@example.com"))


# ── preflight() ──────────────────────────────────────────────────────────────


def test_preflight_collects_command_failures(tmp_path):
    session = FakeDockerSession(
        exec_handlers={"ruff check": DockerError("ruff failed")}
    )
    runner, _ = _make_runner(session=session, tmp_path=tmp_path)

    result = asyncio.run(
        runner.preflight([("ruff", "ruff check ."), ("mypy", "mypy .")])
    )

    assert len(result) == 1
    assert result == [
        PreflightCommandFailure(
            check_name="ruff",
            command="ruff check .",
            output="ruff failed",
        )
    ]
    assert any("ruff check" in c for c in session.exec_calls)
    assert any("mypy" in c for c in session.exec_calls)


def test_preflight_with_empty_checks(tmp_path):
    runner, _ = _make_runner(tmp_path=tmp_path)
    assert asyncio.run(runner.preflight([])) == []


def test_container_runner_uses_global_logs_dir_from_nested_repo(tmp_path):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "config.py").write_text(
        "from pathlib import Path\nlogs_dir = Path('shared-logs')\n"
    )
    project_dir = tmp_path / "My Project"
    project_dir.mkdir()

    cfg = load_config(repo_root=project_dir, global_dir=global_dir)
    runner, _ = _make_runner(name="my-task", cfg=cfg)

    expected = project_dir / "shared-logs" / "my-project"
    assert runner.log_path.parent.resolve() == expected.resolve()
