"""Tests for AgentRunner and FakeAgentRunner."""

import asyncio
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agents.output_protocol import (
    AgentRole,
    CommitMessageOutput,
    CompletionOutput,
    FailedOutput,
    PlannerOutput,
)
from pycastle.agents.result import CancellationToken
from pycastle.agents.runner import AgentRunner, RunRequest
from pycastle.config import Config
from pycastle.errors import (
    AgentFailedError,
    AgentTimeoutError,
    DockerError,
    HardAgentError,
    SetupPhaseError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.session import RoleSession, RunKind
from pycastle.services.agent_service import ParsedTurn, Result
from pycastle.services import CodexService, GitCommandError, GitService, OpenCodeService
from pycastle.iteration._deps import FakeAgentRunner, RecordingStatusDisplay


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def _make_cfg(tmp_path: Path, **kwargs) -> Config:
    """Create a Config with minimal project-local prompt overrides for AgentRunner tests."""
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "plan-prompt.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "_resume-prompt.md").write_text("resume", encoding="utf-8")
    return Config(logs_dir=tmp_path, **kwargs)


def _run_request(*, service: str = "claude", **kwargs) -> RunRequest:
    return RunRequest(service=service, **kwargs)


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

_CODEX_PROTOCOL_ERROR_STREAM = [
    b'{"type":"thread.started","thread_id":"thread-from-fresh"}\n',
    b'{"type":"item.completed","item":{"type":"agent_message",'
    b'"content":"missing required tag"}}\n',
    b'{"type":"turn.completed","usage":{}}\n',
]

_CODEX_PLAN_COMPLETE_STREAM = [
    b'{"type":"item.completed","item":{"type":"agent_message",'
    b'"text":"<plan>{\\"issues\\": [], \\"blocked\\": []}</plan>"}}\n'
]

_OPENCODE_PROTOCOL_ERROR_STREAM = [
    b'{"type":"text","sessionID":"sess-from-fresh",'
    b'"part":{"type":"text","text":"missing required tag",'
    b'"time":{"start":1,"end":2}}}\n',
    b'{"type":"session.status","sessionID":"sess-from-fresh",'
    b'"status":{"type":"idle"}}\n',
]

_OPENCODE_PLAN_COMPLETE_STREAM = [
    b'{"type":"text","sessionID":"sess-from-fresh",'
    b'"part":{"type":"text","text":"<plan>{\\"issues\\": [], \\"blocked\\": []}</plan>",'
    b'"time":{"start":1,"end":2}}}\n',
    b'{"type":"session.status","sessionID":"sess-from-fresh",'
    b'"status":{"type":"idle"}}\n',
]


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
    assert call.template == PromptTemplate.PLAN
    assert call.mount_path == mount
    assert call.scope_args == {
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


def _never_yields():
    """Generator that blocks forever without yielding — simulates a hung agent stream."""
    e = threading.Event()
    e.wait()
    yield  # make this a generator


class _RecordingAgentService:
    def __init__(self, name: str) -> None:
        self.name = name
        self.commands: list[str] = []
        self.env_state_dirs: list[str | None] = []

    def build_command(
        self,
        role: AgentRole,
        model: str,
        effort: str,
        run_kind: RunKind,
        session_uuid: str | None,
    ) -> str:
        command = f"{self.name} exec"
        self.commands.append(command)
        return command

    def build_env(
        self,
        state_dir_container_path: str | None = None,
        token: str | None = None,
    ) -> dict[str, str]:
        self.env_state_dirs.append(state_dir_container_path)
        return {"PYCASTLE_TEST_SERVICE": self.name}

    def run(
        self,
        lines: Iterable[str],
        on_thread_id: Callable[[str], None] | None = None,
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
        return None

    def is_resumable(self, state_dir: Path) -> bool:
        return False

    def valid_efforts(self) -> frozenset[str]:
        return frozenset({"low", "medium", "high"})


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
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


def test_agent_runner_dispatches_with_explicit_claude_service(
    tmp_path,
):
    codex_service = _RecordingAgentService("codex")
    claude_service = _RecordingAgentService("claude")
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
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert claude_service.commands == ["claude exec"]
    assert codex_service.commands == []


def test_agent_runner_uses_requested_service_from_registry(tmp_path):
    claude_service = _RecordingAgentService("claude")
    requested_service = _RecordingAgentService("codex")
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
                mount_path=tmp_path,
                service="codex",
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)
    assert requested_service.commands == ["codex exec"]
    assert claude_service.commands == []


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
                    mount_path=tmp_path,
                    service="codex",
                )
            )
        )

    assert claude_service.commands == []


@pytest.mark.parametrize("service_name", ["claude", "codex"])
def test_agent_runner_uses_universal_image_for_requested_service(
    tmp_path, service_name
):
    requested_service = _RecordingAgentService(service_name)
    docker_client = _make_docker_client([])
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
                mount_path=tmp_path,
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
                RunRequest(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert codex_service.commands == []
    assert codex_service.env_state_dirs == []
    assert claude_service.commands == []
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
                RunRequest(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
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
                RunRequest(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    docker_client.containers.run.assert_not_called()


def test_agent_runner_mixed_services_use_service_command_env_and_parser(
    tmp_path, monkeypatch
):
    from pycastle.services.claude_service import ClaudeService

    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    started: list[tuple[str, dict[str, str]]] = []
    stream_commands: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()

    def _start_container(image_name: str, **kwargs):
        started.append((image_name, dict(kwargs.get("environment") or {})))
        return mock_container

    def _exec_run(cmd, **kwargs):
        if not kwargs.get("stream"):
            return MagicMock(exit_code=0, output=(b"", b""))
        command = cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""
        stream_commands.append(command)
        chunks = _COMPLETE_STREAM if "claude " in command else _CODEX_COMPLETE_STREAM
        return MagicMock(output=iter(chunks))

    mock_client.containers.run.side_effect = _start_container
    mock_container.exec_run.side_effect = _exec_run

    runner = AgentRunner(
        {"GH_TOKEN": "gh-token"},
        _make_cfg(tmp_path, docker_image_name="pycastle-test"),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={
            "claude": ClaudeService(accounts=[("primary", "tok-primary")]),
            "codex": CodexService(),
        },
    )

    implement = asyncio.run(
        runner.run(
            _run_request(
                name="Implement",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
                model="sonnet",
                effort="medium",
                service="claude",
            )
        )
    )
    review = asyncio.run(
        runner.run(
            _run_request(
                name="Review",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.REVIEWER,
                model="gpt-5.3-codex",
                effort="medium",
                service="codex",
            )
        )
    )

    assert isinstance(implement, CommitMessageOutput)
    assert isinstance(review, CommitMessageOutput)
    assert "claude " in stream_commands[0]
    assert "--output-format stream-json" in stream_commands[0]
    assert "codex exec " in stream_commands[1]
    assert "--json" in stream_commands[1]

    assert started[0][0] == "pycastle-test"
    assert started[0][1]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-primary"
    assert "CODEX_HOME" not in started[0][1]
    assert started[1][0] == "pycastle-test"
    assert started[1][1]["TZ"] == "UTC"
    assert started[1][1]["CODEX_HOME"].endswith("/.pycastle-session/reviewer/codex/")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in started[1][1]


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
                    mount_path=tmp_path,
                    token=token,
                )
            )
        )

    mock_client.containers.run.assert_not_called()


def test_agent_runner_run_cancels_token_and_raises_on_usage_limit_in_stream(tmp_path):
    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":429,'
            b'"result":"rate limited"}\n'
        ]
    )
    token = CancellationToken()
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
                    mount_path=tmp_path,
                    token=token,
                )
            )
        )

    assert token.is_cancelled


def test_agent_runner_run_raises_agent_timeout_error_when_retries_exhausted(tmp_path):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            r = MagicMock()
            r.output = _never_yields()
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    cfg = _make_cfg(tmp_path, idle_timeout=0.01, timeout_retries=0)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    with pytest.raises(AgentTimeoutError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
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
        mount_path=tmp_path,
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
    assert err.worktree_path == tmp_path
    assert err.namespace == "test-ns"


def test_agent_runner_run_raises_agent_failed_error_for_protocol_error(tmp_path):
    from unittest.mock import AsyncMock, patch

    runner = AgentRunner({}, _make_cfg(tmp_path), _make_git_service())
    request = _run_request(
        name="Test",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=tmp_path,
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
    assert err.worktree_path == tmp_path
    assert err.namespace == ""


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
        mount_path=tmp_path,
        role=role,
    )

    with pytest.raises(SetupPhaseError) as exc_info:
        asyncio.run(runner.run(request))

    assert exc_info.value.phase == role.value
    assert "pip install failed" in str(exc_info.value)


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
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                )
            )
        )


def test_agent_runner_run_retries_on_timeout_and_returns_output(tmp_path):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    stream_call_count = {"n": 0}

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            stream_call_count["n"] += 1
            r = MagicMock()
            r.output = (
                _never_yields()
                if stream_call_count["n"] == 1
                else iter(_COMPLETE_STREAM)
            )
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    cfg = _make_cfg(tmp_path, idle_timeout=0.01, timeout_retries=1)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


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
                    mount_path=tmp_path,
                )
            )
        )


# ── AgentRunner: status row lifecycle ────────────────────────────────────────


def test_agent_runner_run_registers_and_removes_status_row_on_success(tmp_path):
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                status_display=display,
            )
        )
    )

    assert ("register", "Test", "agent", "started", "Setup") in display.calls
    assert ("remove", "Test", "finished", "success") in display.calls


def test_agent_runner_run_removes_status_row_when_setup_fails(tmp_path):
    git_svc = _make_git_service()
    git_svc.get_user_name.side_effect = RuntimeError("git failure")
    runner = AgentRunner({}, _make_cfg(tmp_path), git_svc, docker_client=MagicMock())
    display = RecordingStatusDisplay()

    with pytest.raises(RuntimeError, match="git failure"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    status_display=display,
                )
            )
        )

    assert ("register", "Test", "agent", "started", "Setup") in display.calls
    assert ("remove", "Test", "failed", "error") in display.calls


def test_agent_runner_run_marks_failed_output_as_failed_in_status_row(tmp_path):
    mock_client = _make_docker_client(_DIVERGENCE_RESOLVER_FAILED_STREAM)
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )
    display = RecordingStatusDisplay()

    with pytest.raises(AgentFailedError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.DIVERGENCE_RESOLVER,
                    status_display=display,
                )
            )
        )

    assert ("register", "Test", "agent", "started", "Setup") in display.calls
    assert ("remove", "Test", "failed", "error") in display.calls


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

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

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

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


def test_agent_runner_run_preflight_returns_empty_list_when_all_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

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
        asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert exc_info.value.phase == "preflight"
    assert "pip install failed" in str(exc_info.value)


def test_agent_runner_run_preflight_returns_failure_tuple_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(
        exit_code=1, stdout=b"E501 line too long"
    )
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert len(result) == 1
    check_name, command, output = result[0]
    assert check_name == "ruff"
    assert command == "ruff check ."
    assert "E501" in output


def test_agent_runner_run_preflight_collects_all_failures_when_multiple_checks_fail(
    tmp_path,
):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"check failed")
    cfg = Config(
        logs_dir=tmp_path,
        preflight_checks=(("ruff", "ruff check ."), ("mypy", "mypy .")),
    )
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert len(result) == 2
    assert result[0][0] == "ruff"
    assert result[1][0] == "mypy"


def test_agent_runner_run_preflight_stops_container_after_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client()
    cfg = _make_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    mock_client.containers.run.return_value.stop.assert_called()


def test_agent_runner_run_preflight_stops_container_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"check failed")
    cfg = _make_cfg(tmp_path, preflight_checks=(("lint", "lint ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

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
        asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert exc_info.value.phase == "preflight"


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

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

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

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

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
            name="preflight-checks", mount_path=tmp_path, status_display=display
        )
    )

    assert (
        "register",
        "preflight-checks",
        "agent",
        "started",
        "Setup",
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
            name="preflight-checks", mount_path=tmp_path, status_display=display
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
            name="preflight-checks", mount_path=tmp_path, status_display=display
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
                name="preflight-checks", mount_path=tmp_path, status_display=display
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
            name="preflight-checks", mount_path=tmp_path, status_display=display
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
        asyncio.run(runner.run_preflight(name="preflight-checks", mount_path=tmp_path))


# ── RunRequest: core interface ────────────────────────────────────────────────


def test_run_request_stores_required_fields():
    from pycastle.agents.output_protocol import AgentRole

    req = _run_request(
        name="Agent",
        template=PromptTemplate.PLAN,
        mount_path=Path("/workspace"),
    )
    assert req.name == "Agent"
    assert req.template == PromptTemplate.PLAN
    assert req.mount_path == Path("/workspace")
    assert req.role == AgentRole.IMPLEMENTER
    assert req.scope_args is None
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
                mount_path=tmp_path,
            )
        )
    )

    assert captured_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_cancels_token_and_raises_on_transient_agent_error(tmp_path):
    """TransientAgentError from a 5xx result cancels the CancellationToken and re-raises."""
    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":529,'
            b'"result":"API Error: 529 Overloaded"}\n'
        ]
    )
    token = CancellationToken()
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    with pytest.raises(TransientAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Implement Agent #42",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    token=token,
                )
            )
        )

    assert token.is_cancelled


def test_agent_runner_does_not_call_mark_exhausted_on_transient_agent_error(tmp_path):
    """TransientAgentError must NOT mark the account exhausted (server-wide, not account-specific)."""
    from pycastle.services.claude_service import ClaudeService

    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":529,'
            b'"result":"API Error: 529 Overloaded"}\n'
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

    with pytest.raises(TransientAgentError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    # Account must still be available — mark_exhausted was NOT called
    assert svc.is_available() is True


def test_agent_runner_marks_picked_token_exhausted_on_usage_limit(tmp_path):
    from pycastle.services.claude_service import ClaudeService

    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":429,'
            b'"result":"rate limited"}\n'
        ]
    )

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    # secondary was picked (highest priority) and marked exhausted; primary should now be available
    assert svc.is_available() is True
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-primary"


def test_agent_runner_marks_picked_token_permanently_exhausted_on_subscription_access_denial(
    tmp_path,
):
    from pycastle.services.claude_service import ClaudeService

    denial = (
        "Your organization has disabled Claude subscription access for Claude Code. "
        "Please use an Anthropic API key instead, or ask your admin to enable "
        "Claude subscription access for Claude Code."
    )
    mock_client = _make_docker_client(
        [
            (
                b'{"type":"result","is_error":true,"api_error_status":403,'
                b'"result":"' + denial.encode() + b'"}\n'
            )
        ]
    )

    svc = ClaudeService(
        accounts=[("secondary", "tok-secondary"), ("primary", "tok-primary")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"claude": svc},
    )

    with pytest.raises(UsageLimitError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert exc_info.value.is_permanent is True
    assert exc_info.value.account_label == "secondary"
    env = svc.build_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-primary"


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


# ── AgentRunner: CLAUDE_CONFIG_DIR injection ──────────────────────────────────


def test_agent_runner_injects_claude_config_dir_for_implementer(tmp_path):
    """AgentRunner.run() must set CLAUDE_CONFIG_DIR to the role session dir inside the worktree."""
    from pycastle.agents.output_protocol import AgentRole

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

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
            )
        )
    )

    assert "CLAUDE_CONFIG_DIR" in captured_env
    assert captured_env["CLAUDE_CONFIG_DIR"].endswith(
        "/.pycastle-session/implementer/claude/"
    )


# ── AgentRunner: namespaced session dir ───────────────────────────────────────


def test_agent_runner_injects_namespaced_claude_config_dir_when_session_namespace_set(
    tmp_path,
):
    """When session_namespace is set, CLAUDE_CONFIG_DIR must include the namespace subdir."""
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

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
                session_namespace="main",
            )
        )
    )

    assert "CLAUDE_CONFIG_DIR" in captured_env
    assert captured_env["CLAUDE_CONFIG_DIR"].endswith(
        "/.pycastle-session/implementer/main/claude/"
    )


def test_agent_runner_uses_namespace_subdir_for_resume_check(tmp_path):
    """When session_namespace is set, Fresh/Resume decision uses the namespaced service subdir."""
    # Seed the namespaced claude service dir (not the role-level dir)
    namespace_dir = tmp_path / ".pycastle-session" / "implementer" / "main" / "claude"
    namespace_dir.mkdir(parents=True)
    (namespace_dir / "session.jsonl").write_text("{}")

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
                session_namespace="main",
            )
        )
    )

    # Namespaced dir was non-empty → should Resume
    assert captured_cmds, "No streaming exec recorded"
    assert any("--resume" in c for c in captured_cmds)


def test_agent_runner_uses_fresh_for_different_namespace_when_other_namespace_has_session(
    tmp_path,
):
    """When the 'issues' namespace has a session, a 'main' namespace run must still be Fresh."""
    # Seed the 'issues' namespace claude service dir only
    issues_dir = tmp_path / ".pycastle-session" / "implementer" / "issues" / "claude"
    issues_dir.mkdir(parents=True)
    (issues_dir / "session.jsonl").write_text("{}")

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
                session_namespace="main",
            )
        )
    )

    # 'main' namespace dir was empty → should be Fresh
    assert captured_cmds, "No streaming exec recorded"
    assert any("--session-id" in c for c in captured_cmds)
    assert all("--resume" not in c for c in captured_cmds)


# ── AgentRunner: session-id in claude command ─────────────────────────────────


def test_agent_runner_passes_session_id_flag_to_claude_on_fresh_run(tmp_path):
    """On a Fresh run AgentRunner must invoke claude with --session-id <uuid>."""
    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert captured_cmds, "No streaming exec recorded"
    assert any("--session-id" in c for c in captured_cmds)
    assert all("--resume" not in c for c in captured_cmds)


def test_agent_runner_records_claude_service_session_metadata_on_success(tmp_path):
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert session.service_session_metadata("claude") == {
        "service": "claude",
        "provider_session_id": session.session_uuid(),
    }


def test_agent_runner_records_codex_service_session_metadata_on_success(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_CODEX_COMPLETE_STREAM),
        service_registry={"codex": CodexService()},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Codex",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                service="codex",
            )
        )
    )

    session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert session.service_session_metadata("codex") == {
        "service": "codex",
        "provider_session_id": "thread-from-fresh",
    }


def _seed_implementer_session(tmp_path: Path) -> None:
    """Seed the claude service state dir so ClaudeService.is_resumable returns True."""
    claude_dir = tmp_path / ".pycastle-session" / "implementer" / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "session.json").write_text("{}")


def test_agent_runner_passes_resume_flag_to_claude_when_session_exists(tmp_path):
    """On a Resume run AgentRunner must invoke claude with --resume <uuid>."""
    from pycastle.agents.output_protocol import AgentRole

    _seed_implementer_session(tmp_path)

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
            )
        )
    )

    assert captured_cmds, "No streaming exec recorded"
    assert any("--resume" in c for c in captured_cmds)
    assert all("--session-id" not in c for c in captured_cmds)


# ── AgentRunner: non-typed Resume retry ───────────────────────────────────────


def _make_docker_client_with_controlled_streams(
    stream_responses: list,
) -> MagicMock:
    """Mock docker client whose nth streaming exec_run returns or raises stream_responses[n]."""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    responses = iter(stream_responses)

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            response = next(responses, RuntimeError("unexpected call"))
            if isinstance(response, BaseException):
                raise response
            r = MagicMock()
            r.output = iter(response)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    return mock_client


def test_resume_run_non_typed_exception_retries_same_session_and_succeeds(tmp_path):
    """On a Resume run, a non-typed exception triggers one in-call retry; success on retry returns output."""
    _seed_implementer_session(tmp_path)

    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("transient error"), _COMPLETE_STREAM]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


def test_resume_run_consecutive_non_typed_exceptions_raise_agent_failed_error(tmp_path):
    """On a Resume run, two consecutive non-typed exceptions cause AgentRunner.run to raise AgentFailedError."""
    _seed_implementer_session(tmp_path)

    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("first failure"), RuntimeError("second failure")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    with pytest.raises(AgentFailedError) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Impl",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert exc_info.value.failure_class == "non_typed_crash"


def test_resume_run_non_typed_exception_does_not_wipe_session(tmp_path):
    """On consecutive non-typed exceptions during a Resume run, start_fresh is not called — session dir is preserved."""
    _seed_implementer_session(tmp_path)
    session_file = (
        tmp_path / ".pycastle-session" / "implementer" / "claude" / "session.json"
    )
    assert session_file.exists()

    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("first failure"), RuntimeError("second failure")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    with pytest.raises(AgentFailedError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Impl",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert session_file.exists(), (
        "session.json was wiped but should have been preserved"
    )


def test_fresh_run_non_typed_exception_propagates(tmp_path):
    """A non-typed exception on a Fresh run (no existing session) propagates immediately."""
    mock_client = _make_docker_client_with_controlled_streams(
        [RuntimeError("docker failure")]
    )
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    with pytest.raises(RuntimeError, match="docker failure"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Impl",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )


# ── AgentRunner: Reviewer resume parity ───────────────────────────────────────


def _seed_reviewer_session(tmp_path: Path) -> None:
    """Seed the reviewer claude service state dir so ClaudeService.is_resumable returns True."""
    claude_dir = tmp_path / ".pycastle-session" / "reviewer" / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "session.json").write_text("{}")


def test_agent_runner_injects_claude_config_dir_for_reviewer(tmp_path):
    """AgentRunner.run() must set CLAUDE_CONFIG_DIR to the reviewer session dir."""
    from pycastle.agents.output_protocol import AgentRole

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
            r.output = iter(_REVIEWER_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Review",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.REVIEWER,
            )
        )
    )

    assert "CLAUDE_CONFIG_DIR" in captured_env
    assert captured_env["CLAUDE_CONFIG_DIR"].endswith(
        "/.pycastle-session/reviewer/claude/"
    )


def test_agent_runner_passes_resume_flag_to_claude_when_reviewer_session_exists(
    tmp_path,
):
    """On a Reviewer Resume run AgentRunner must invoke claude with --resume <uuid>."""
    from pycastle.agents.output_protocol import AgentRole

    _seed_reviewer_session(tmp_path)

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(_REVIEWER_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Review",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.REVIEWER,
            )
        )
    )

    assert captured_cmds, "No streaming exec recorded"
    assert any("--resume" in c for c in captured_cmds)
    assert all("--session-id" not in c for c in captured_cmds)


# ── AgentRunner: Merger resume parity ─────────────────────────────────────────


def _seed_merger_session(tmp_path: Path) -> None:
    """Seed the merger claude service state dir so ClaudeService.is_resumable returns True."""
    claude_dir = tmp_path / ".pycastle-session" / "merger" / "claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "session.json").write_text("{}")


def test_agent_runner_passes_resume_flag_to_claude_when_merger_session_exists(tmp_path):
    """On a Merger Resume run AgentRunner must invoke claude with --resume <uuid>."""
    from pycastle.agents.output_protocol import AgentRole

    _seed_merger_session(tmp_path)

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(_MERGER_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Merge",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.MERGER,
            )
        )
    )

    assert captured_cmds, "No streaming exec recorded"
    assert any("--resume" in c for c in captured_cmds)
    assert all("--session-id" not in c for c in captured_cmds)


# ── AgentRunner: _build_prompt ────────────────────────────────────────────────


async def _noop_exec(cmd: str) -> str:
    return ""


def _make_build_prompt_cfg(tmp_path: Path) -> Config:
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "plan-prompt.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "_resume-prompt.md").write_text("resume-content", encoding="utf-8")
    return Config(logs_dir=tmp_path)


def test_build_prompt_uses_resume_template_on_resume_without_role_flag(tmp_path):
    """On a Resume run without send_role_prompt_on_resume, _build_prompt uses the RESUME template."""
    cfg = _make_build_prompt_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    result = asyncio.run(
        runner._build_prompt(
            _PLAN_TEMPLATE,
            _PLAN_SCOPE_ARGS,
            _noop_exec,
            run_kind=RunKind.RESUME,
            send_role_prompt_on_resume=False,
        )
    )

    assert result == "resume-content"


def test_build_prompt_uses_role_template_on_resume_with_send_role_prompt(tmp_path):
    """On a Resume run with send_role_prompt_on_resume=True, _build_prompt uses the role template."""
    cfg = _make_build_prompt_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    result = asyncio.run(
        runner._build_prompt(
            _PLAN_TEMPLATE,
            _PLAN_SCOPE_ARGS,
            _noop_exec,
            run_kind=RunKind.RESUME,
            send_role_prompt_on_resume=True,
        )
    )

    assert result == "[] []"


def test_build_prompt_uses_role_template_on_fresh_run(tmp_path):
    """On a Fresh run, _build_prompt renders the role template."""
    cfg = _make_build_prompt_cfg(tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    result = asyncio.run(
        runner._build_prompt(
            _PLAN_TEMPLATE,
            _PLAN_SCOPE_ARGS,
            _noop_exec,
            run_kind=RunKind.FRESH,
            send_role_prompt_on_resume=False,
        )
    )

    assert result == "[] []"


def test_build_prompt_expands_shell_expressions_via_container_exec(tmp_path):
    """_build_prompt passes container_exec to the renderer for shell expression expansion."""
    prompts_dir = tmp_path / "pycastle" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "_resume-prompt.md").write_text(
        "Result: !`echo hi`", encoding="utf-8"
    )
    cfg = Config(logs_dir=tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service())

    async def fake_exec(cmd: str) -> str:
        if "echo hi" in cmd:
            return "expanded\n"
        return ""

    result = asyncio.run(
        runner._build_prompt(
            PromptTemplate.RESUME,
            {},
            fake_exec,
            run_kind=RunKind.RESUME,
            send_role_prompt_on_resume=False,
        )
    )

    assert result == "Result: expanded"


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
                    mount_path=tmp_path,
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
                    mount_path=tmp_path,
                )
            )
        )

    # Account must still be available — mark_exhausted was NOT called
    assert svc.is_available() is True


def test_agent_runner_seeds_codex_auth_for_fresh_state_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    mock_client = _make_docker_client(_CODEX_COMPLETE_STREAM)
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"codex": CodexService()},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Codex",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                service="codex",
            )
        )
    )

    seeded = tmp_path / ".pycastle-session" / "implementer" / "codex" / "auth.json"
    assert seeded.read_text(encoding="utf-8") == '{"mode":"oauth"}'


def test_agent_runner_codex_reprompt_resumes_with_captured_thread_id(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    host_auth = home / ".codex" / "auth.json"
    host_auth.parent.mkdir(parents=True)
    host_auth.write_text('{"mode":"oauth"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    streams = iter([_CODEX_PROTOCOL_ERROR_STREAM, _CODEX_PLAN_COMPLETE_STREAM])

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(next(streams))
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"codex": CodexService()},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="Codex",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service="codex",
            )
        )
    )

    assert isinstance(result, PlannerOutput)
    assert "codex exec " in captured_cmds[0]
    assert "resume" not in captured_cmds[0]
    assert "codex exec resume thread-from-fresh" in captured_cmds[1]
    saved = tmp_path / ".pycastle-session" / "planner" / "codex" / "thread_id"
    assert saved.read_text(encoding="utf-8") == "thread-from-fresh"


def test_agent_runner_opencode_reprompt_resumes_with_persisted_session_id(tmp_path):
    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    streams = iter([_OPENCODE_PROTOCOL_ERROR_STREAM, _OPENCODE_PLAN_COMPLETE_STREAM])

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.output = iter(next(streams))
            return result
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"opencode": OpenCodeService()},
    )

    result = asyncio.run(
        runner.run(
            _run_request(
                name="OpenCode",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service="opencode",
            )
        )
    )

    assert isinstance(result, PlannerOutput)
    assert "opencode run --format json " in captured_cmds[0]
    assert "--session" not in captured_cmds[0]
    assert "--session sess-from-fresh" in captured_cmds[1]
    saved = tmp_path / ".pycastle-session" / "planner" / "opencode" / "session_id"
    assert saved.read_text(encoding="utf-8") == "sess-from-fresh"


def test_agent_runner_records_opencode_service_session_metadata_on_success(tmp_path):
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_OPENCODE_PLAN_COMPLETE_STREAM),
        service_registry={"opencode": OpenCodeService()},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="OpenCode",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service="opencode",
            )
        )
    )

    session = RoleSession(tmp_path, AgentRole.PLANNER)
    assert session.service_session_metadata("opencode") == {
        "service": "opencode",
        "provider_session_id": "sess-from-fresh",
    }


def test_agent_runner_does_not_record_metadata_on_failed_run(tmp_path):
    # Stream with no <plan> tag forces PlanParseError on every attempt; after 3
    # retries the runner returns FailedOutput without saving metadata.
    no_plan_stream = [
        b'{"type": "result", "result": "no plan tag here", "is_error": false}\n'
    ]
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(no_plan_stream),
    )

    with pytest.raises(AgentFailedError):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Planner",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    role=AgentRole.PLANNER,
                )
            )
        )

    session = RoleSession(tmp_path, AgentRole.PLANNER)
    assert session.service_session_metadata("claude") is None


def test_agent_runner_opencode_resume_uses_persisted_session_id_on_later_run(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "planner" / "opencode"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("sess-from-prior-run", encoding="utf-8")

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.output = iter(_OPENCODE_PLAN_COMPLETE_STREAM)
            return result
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"opencode": OpenCodeService()},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="OpenCode",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.PLANNER,
                service="opencode",
            )
        )
    )

    assert captured_cmds == [
        'export PATH="/home/agent/.local/bin:$PATH"; opencode run --format json --session sess-from-prior-run "$(cat /tmp/.pycastle_prompt)"'
    ]


def test_agent_runner_opencode_keeps_api_key_out_of_session_files_across_resume_runs(
    tmp_path,
):
    service = OpenCodeService(api_key="go-key")
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_OPENCODE_PLAN_COMPLETE_STREAM),
        service_registry={"opencode": service},
    )

    request = _run_request(
        name="OpenCode",
        template=_PLAN_TEMPLATE,
        scope_args=_PLAN_SCOPE_ARGS,
        mount_path=tmp_path,
        role=AgentRole.PLANNER,
        service="opencode",
    )
    asyncio.run(runner.run(request))
    asyncio.run(runner.run(request))

    session_files = sorted(
        path for path in (tmp_path / ".pycastle-session").rglob("*") if path.is_file()
    )

    assert session_files == [
        tmp_path / ".pycastle-session" / "planner" / "_service_session_metadata.json",
        tmp_path / ".pycastle-session" / "planner" / "opencode" / "session_id",
    ]
    assert session_files[1].read_text(encoding="utf-8") == "sess-from-fresh"
    assert all(
        "go-key" not in path.read_text(encoding="utf-8") for path in session_files
    )


def test_agent_runner_codex_resume_uses_thread_id_from_rollout(tmp_path):
    state_dir = tmp_path / ".pycastle-session" / "implementer" / "codex"
    sessions_dir = state_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (state_dir / "auth.json").write_text('{"mode":"oauth"}', encoding="utf-8")
    (sessions_dir / "rollout-001.jsonl").write_text(
        '{"type":"thread.started","thread_id":"thread-from-rollout"}\n',
        encoding="utf-8",
    )

    captured_cmds: list[str] = []
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if kwargs.get("stream"):
            captured_cmds.append(cmd)
            r = MagicMock()
            r.output = iter(_CODEX_COMPLETE_STREAM)
            return r
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        service_registry={"codex": CodexService()},
    )

    asyncio.run(
        runner.run(
            _run_request(
                name="Codex",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                service="codex",
            )
        )
    )

    assert captured_cmds
    assert "codex exec resume thread-from-rollout" in captured_cmds[0]


def test_agent_runner_codex_missing_host_auth_raises_hard_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_CODEX_COMPLETE_STREAM),
        service_registry={"codex": CodexService()},
    )

    with pytest.raises(
        HardAgentError, match="Codex authentication missing"
    ) as exc_info:
        asyncio.run(
            runner.run(
                _run_request(
                    name="Codex",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    service="codex",
                )
            )
        )

    assert exc_info.value.status_code == 401


def test_agent_runner_codex_missing_host_auth_leaves_no_done_session_state(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_docker_client(_CODEX_COMPLETE_STREAM),
        service_registry={"codex": CodexService()},
    )

    with pytest.raises(HardAgentError, match="Codex authentication missing"):
        asyncio.run(
            runner.run(
                _run_request(
                    name="Codex",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                    service="codex",
                )
            )
        )

    session = RoleSession(tmp_path, AgentRole.IMPLEMENTER)
    assert session.is_done() is False
    assert not session.path.exists()


# ── AgentRunner: protocol-error retry semantics ───────────────────────────────


def _make_setup_docker_client() -> MagicMock:
    """Mock docker client that handles container start and non-streaming setup calls."""
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    mock_container.exec_run.return_value = MagicMock(exit_code=0, output=(b"", b""))
    return mock_client


def test_agent_runner_run_returns_success_after_protocol_error_on_first_attempt(
    tmp_path,
):
    from unittest.mock import patch
    from pycastle.agents.output_protocol import PlanParseError
    from pycastle.agents.runner import REPROMPT_MESSAGE
    from pycastle.infrastructure.container_runner import ContainerRunner

    success_output = CommitMessageOutput(message="done")
    work_calls: list[tuple[str, RunKind]] = []

    async def _fake_work(role, prompt, *, run_kind, session_uuid):
        work_calls.append((prompt, run_kind))
        if len(work_calls) == 1:
            raise PlanParseError("no tag")
        return success_output

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        result = asyncio.run(
            runner.run(
                _run_request(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )

    assert isinstance(result, CommitMessageOutput)
    assert len(work_calls) == 2
    assert work_calls[1][0] == REPROMPT_MESSAGE
    assert work_calls[1][1] == RunKind.RESUME


def test_agent_runner_run_raises_agent_failed_error_after_three_protocol_errors(
    tmp_path,
):
    from unittest.mock import patch
    from pycastle.agents.output_protocol import PromiseParseError
    from pycastle.infrastructure.container_runner import ContainerRunner

    call_count = 0

    async def _fake_work(role, prompt, *, run_kind, session_uuid):
        nonlocal call_count
        call_count += 1
        raise PromiseParseError("no tag")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        with pytest.raises(AgentFailedError) as exc_info:
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=tmp_path,
                    )
                )
            )

    assert exc_info.value.failure_class == "protocol_error"
    assert call_count == 3


def test_agent_runner_run_does_not_reprompt_when_work_returns_failed_output(tmp_path):
    from unittest.mock import patch
    from pycastle.infrastructure.container_runner import ContainerRunner

    call_count = 0

    async def _fake_work(role, prompt, *, run_kind, session_uuid):
        nonlocal call_count
        call_count += 1
        return FailedOutput(failure_class="agent_failed")

    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=_make_setup_docker_client(),
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        with pytest.raises(AgentFailedError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=tmp_path,
                    )
                )
            )

    assert call_count == 1


def test_agent_runner_run_decrements_timeout_budget_when_protocol_error_precedes_timeout(
    tmp_path,
):
    from unittest.mock import patch
    from pycastle.agents.output_protocol import PlanParseError
    from pycastle.infrastructure.container_runner import ContainerRunner

    call_count = 0

    async def _fake_work(role, prompt, *, run_kind, session_uuid):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise PlanParseError("no tag")
        raise AgentTimeoutError("timeout")

    cfg = _make_cfg(tmp_path, timeout_retries=0)
    runner = AgentRunner(
        {}, cfg, _make_git_service(), docker_client=_make_setup_docker_client()
    )

    with patch.object(ContainerRunner, "work", side_effect=_fake_work):
        with pytest.raises(AgentTimeoutError):
            asyncio.run(
                runner.run(
                    _run_request(
                        name="Test",
                        template=_PLAN_TEMPLATE,
                        scope_args=_PLAN_SCOPE_ARGS,
                        mount_path=tmp_path,
                    )
                )
            )
