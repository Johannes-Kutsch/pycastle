"""Tests for AgentRunner and FakeAgentRunner."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agent_output_protocol import (
    AgentRole,
    CommitMessageOutput,
    CompletionOutput,
    FailedOutput,
)
from pycastle.agent_result import CancellationToken
from pycastle.agent_runner import AgentRunner, RunRequest
from pycastle.config import Config
from pycastle.errors import (
    AgentTimeoutError,
    DockerError,
    UsageLimitError,
)
from pycastle.prompt_pipeline import PromptTemplate
from pycastle.session_resume import RunKind
from pycastle.services import GitCommandError, GitService
from pycastle.iteration._deps import FakeAgentRunner, RecordingStatusDisplay


def _make_cfg(tmp_path: Path, **kwargs) -> Config:
    """Create a Config with a minimal prompts_dir for AgentRunner tests."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "plan-prompt.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "_resume-prompt.md").write_text("resume", encoding="utf-8")
    return Config(logs_dir=tmp_path, prompts_dir=prompts_dir, **kwargs)


_PLAN_TEMPLATE = PromptTemplate.PLAN
_PLAN_SCOPE_ARGS = {"ALL_OPEN_ISSUES_JSON": "[]", "READY_FOR_AGENT_ISSUES_JSON": "[]"}

# A minimal NDJSON stream that process_stream accepts as CommitMessageOutput (IMPLEMENTER/REVIEWER role)
_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<commit_message>done</commit_message>", "is_error": false}\n'
]

# A minimal NDJSON stream that process_stream accepts as CompletionOutput (MERGER/IMPROVE role)
_MERGER_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<promise>COMPLETE</promise>", "is_error": false}\n'
]


# ── FakeAgentRunner: queue behaviour ─────────────────────────────────────────


def test_fake_agent_runner_returns_queued_completion_output():
    fake = FakeAgentRunner([CompletionOutput()])
    result = asyncio.run(
        fake.run(
            RunRequest(
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
                RunRequest(
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
                RunRequest(
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
                RunRequest(
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
            await run(RunRequest(name="A", template=_PLAN_TEMPLATE, mount_path=m)),
            await run(RunRequest(name="B", template=_PLAN_TEMPLATE, mount_path=m)),
            await run(RunRequest(name="C", template=_PLAN_TEMPLATE, mount_path=m)),
        ]

    results = asyncio.run(_collect())
    assert results == [r1, r2, r3]


def test_fake_agent_runner_records_all_calls():
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    mount = Path("/workspace")

    asyncio.run(
        fake.run(RunRequest(name="X", template=_PLAN_TEMPLATE, mount_path=mount))
    )
    asyncio.run(
        fake.run(RunRequest(name="Y", template=_PLAN_TEMPLATE, mount_path=mount))
    )

    assert len(fake.calls) == 2
    assert fake.calls[0].name == "X"
    assert fake.calls[1].name == "Y"


def test_fake_agent_runner_records_call_kwargs():
    fake = FakeAgentRunner([CompletionOutput()])
    mount = Path("/workspace")

    asyncio.run(
        fake.run(
            RunRequest(
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
            RunRequest(
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
                RunRequest(name="Agent", template=_PLAN_TEMPLATE, mount_path=Path("/w"))
            )
        )


def test_fake_agent_runner_side_effect_still_records_calls():
    async def _effect(request: RunRequest):
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_effect)
    asyncio.run(
        fake.run(
            RunRequest(name="Recorded", template=_PLAN_TEMPLATE, mount_path=Path("/w"))
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
            RunRequest(name="Agent", template=_PLAN_TEMPLATE, mount_path=Path("/w"))
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


# ── AgentRunner: run() return values ─────────────────────────────────────────


def test_agent_runner_run_returns_agent_output(tmp_path):
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {}, _make_cfg(tmp_path), _make_git_service(), docker_client=mock_client
    )

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


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
                RunRequest(
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
                RunRequest(
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
                RunRequest(
                    name="Test",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
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
            RunRequest(
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
                RunRequest(
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
            RunRequest(
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
                RunRequest(
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


def test_agent_runner_run_preflight_returns_empty_list_when_all_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = _make_cfg(tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


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


def test_agent_runner_run_preflight_propagates_docker_error_when_pip_install_fails(
    tmp_path,
):
    # Reproduces the issue-342 silent-swallow bug: if pip install fails, DockerError
    # must propagate out of run_preflight rather than continuing with ruff absent.
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

    with pytest.raises(DockerError):
        asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))


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
    assert ("remove", "preflight-checks", "finished", "success") in display.calls


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


def test_agent_runner_run_preflight_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    runner = AgentRunner({}, _make_cfg(tmp_path), mock_git, docker_client=MagicMock())

    with pytest.raises(GitCommandError):
        asyncio.run(runner.run_preflight(name="preflight-checks", mount_path=tmp_path))


# ── RunRequest: core interface ────────────────────────────────────────────────


def test_run_request_stores_required_fields():
    from pycastle.agent_output_protocol import AgentRole

    req = RunRequest(
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
    req = RunRequest(
        name="Agent",
        template=PromptTemplate.PLAN,
        mount_path=Path("/workspace"),
        session_namespace="main",
    )
    assert req.session_namespace == "main"


# ── AgentRunner: AccountPool integration ─────────────────────────────────────


def test_agent_runner_injects_picked_token_into_container_env(tmp_path):
    from pycastle.account_pool import AccountPool

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

    pool = AccountPool([("secondary", "tok-secondary"), ("primary", "tok-primary")])
    runner = AgentRunner(
        {"GH_TOKEN": "gh"},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        account_pool=pool,
    )

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

    assert captured_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-secondary"


def test_agent_runner_marks_picked_token_exhausted_on_usage_limit(tmp_path):
    from datetime import datetime

    from pycastle.account_pool import AccountPool

    mock_client = _make_docker_client(
        [
            b'{"type":"result","is_error":true,"api_error_status":429,'
            b'"result":"rate limited"}\n'
        ]
    )

    pool = AccountPool([("secondary", "tok-secondary"), ("primary", "tok-primary")])
    runner = AgentRunner(
        {},
        _make_cfg(tmp_path),
        _make_git_service(),
        docker_client=mock_client,
        account_pool=pool,
    )

    fixed_now = datetime(2026, 1, 1, 14, 0, 0)
    with pytest.raises(UsageLimitError):
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

    # secondary was picked (highest priority); now exhausted, so primary should be next
    name, tok = pool.pick(now=fixed_now)
    assert name == "primary"
    assert tok == "tok-primary"


def test_fake_agent_runner_accepts_run_request_and_records_it():
    completion = CompletionOutput()
    fake = FakeAgentRunner([completion])
    req = RunRequest(
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
    from pycastle.agent_output_protocol import AgentRole

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
            RunRequest(
                name="Test",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.IMPLEMENTER,
            )
        )
    )

    assert "CLAUDE_CONFIG_DIR" in captured_env
    assert captured_env["CLAUDE_CONFIG_DIR"].endswith("/.pycastle-session/implementer/")


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
            RunRequest(
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
        "/.pycastle-session/implementer/main/"
    )


def test_agent_runner_uses_namespace_subdir_for_resume_check(tmp_path):
    """When session_namespace is set, Fresh/Resume decision uses the namespaced subdir."""
    # Seed the namespaced session dir (not the role-level dir)
    namespace_dir = tmp_path / ".pycastle-session" / "implementer" / "main"
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
            RunRequest(
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
    # Seed the 'issues' namespace dir only
    issues_dir = tmp_path / ".pycastle-session" / "implementer" / "issues"
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
            RunRequest(
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
            RunRequest(
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


def _seed_implementer_session(tmp_path: Path) -> None:
    """Seed an implementer session dir so has_resumable_session returns True."""
    role_dir = tmp_path / ".pycastle-session" / "implementer"
    role_dir.mkdir(parents=True)
    (role_dir / "session.json").write_text("{}")


def test_agent_runner_passes_resume_flag_to_claude_when_session_exists(tmp_path):
    """On a Resume run AgentRunner must invoke claude with --resume <uuid>."""
    from pycastle.agent_output_protocol import AgentRole

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
            RunRequest(
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
            RunRequest(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, CommitMessageOutput)


def test_resume_run_consecutive_non_typed_exceptions_return_failed_output(tmp_path):
    """On a Resume run, two consecutive non-typed exceptions cause the call to return FailedOutput."""
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

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Impl",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
            )
        )
    )

    assert isinstance(result, FailedOutput)


def test_resume_run_non_typed_exception_does_not_wipe_session(tmp_path):
    """On consecutive non-typed exceptions during a Resume run, start_fresh is not called — session dir is preserved."""
    _seed_implementer_session(tmp_path)
    session_file = tmp_path / ".pycastle-session" / "implementer" / "session.json"
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

    asyncio.run(
        runner.run(
            RunRequest(
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
                RunRequest(
                    name="Impl",
                    template=_PLAN_TEMPLATE,
                    scope_args=_PLAN_SCOPE_ARGS,
                    mount_path=tmp_path,
                )
            )
        )


# ── AgentRunner: Reviewer resume parity ───────────────────────────────────────


def _seed_reviewer_session(tmp_path: Path) -> None:
    """Seed a reviewer session dir so has_resumable_session returns True."""
    role_dir = tmp_path / ".pycastle-session" / "reviewer"
    role_dir.mkdir(parents=True)
    (role_dir / "session.json").write_text("{}")


def test_agent_runner_injects_claude_config_dir_for_reviewer(tmp_path):
    """AgentRunner.run() must set CLAUDE_CONFIG_DIR to the reviewer session dir."""
    from pycastle.agent_output_protocol import AgentRole

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
            RunRequest(
                name="Review",
                template=_PLAN_TEMPLATE,
                scope_args=_PLAN_SCOPE_ARGS,
                mount_path=tmp_path,
                role=AgentRole.REVIEWER,
            )
        )
    )

    assert "CLAUDE_CONFIG_DIR" in captured_env
    assert captured_env["CLAUDE_CONFIG_DIR"].endswith("/.pycastle-session/reviewer/")


def test_agent_runner_passes_resume_flag_to_claude_when_reviewer_session_exists(
    tmp_path,
):
    """On a Reviewer Resume run AgentRunner must invoke claude with --resume <uuid>."""
    from pycastle.agent_output_protocol import AgentRole

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
            RunRequest(
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
    """Seed a merger session dir so has_resumable_session returns True."""
    role_dir = tmp_path / ".pycastle-session" / "merger"
    role_dir.mkdir(parents=True)
    (role_dir / "session.json").write_text("{}")


def test_agent_runner_passes_resume_flag_to_claude_when_merger_session_exists(tmp_path):
    """On a Merger Resume run AgentRunner must invoke claude with --resume <uuid>."""
    from pycastle.agent_output_protocol import AgentRole

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
            RunRequest(
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
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "plan-prompt.md").write_text(
        "{{ALL_OPEN_ISSUES_JSON}} {{READY_FOR_AGENT_ISSUES_JSON}}", encoding="utf-8"
    )
    (prompts_dir / "_resume-prompt.md").write_text("resume-content", encoding="utf-8")
    return Config(logs_dir=tmp_path, prompts_dir=prompts_dir)


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
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "_resume-prompt.md").write_text(
        "Result: !`echo hi`", encoding="utf-8"
    )
    cfg = Config(logs_dir=tmp_path, prompts_dir=prompts_dir)
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
