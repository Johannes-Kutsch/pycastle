"""Tests for AgentRunner and FakeAgentRunner."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agent_output_protocol import CompletionOutput
from pycastle.agent_result import CancellationToken, PreflightFailure
from pycastle.agent_runner import AgentRunner, RunRequest
from pycastle.config import Config
from pycastle.errors import (
    AgentTimeoutError,
    DockerError,
    UsageLimitError,
)
from pycastle.services import GitCommandError, GitService
from pycastle.iteration._deps import FakeAgentRunner, RecordingStatusDisplay

# A minimal NDJSON stream that process_stream accepts as CompletionOutput (IMPLEMENTER role)
_COMPLETE_STREAM = [
    b'{"type": "result", "result": "<promise>COMPLETE</promise>", "is_error": false}\n'
]


# ── FakeAgentRunner: queue behaviour ─────────────────────────────────────────


def test_fake_agent_runner_returns_queued_completion_output():
    fake = FakeAgentRunner([CompletionOutput()])
    result = asyncio.run(
        fake.run(
            RunRequest(
                name="Tester",
                prompt_file=Path("/prompt.md"),
                mount_path=Path("/workspace"),
            )
        )
    )
    assert isinstance(result, CompletionOutput)


def test_fake_agent_runner_returns_queued_preflight_failure():
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    fake = FakeAgentRunner([failure])
    result = asyncio.run(
        fake.run(
            RunRequest(
                name="Tester",
                prompt_file=Path("/prompt.md"),
                mount_path=Path("/workspace"),
            )
        )
    )
    assert result is failure


def test_fake_agent_runner_raises_queued_exception():
    fake = FakeAgentRunner([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            fake.run(
                RunRequest(
                    name="Tester",
                    prompt_file=Path("/prompt.md"),
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
                    prompt_file=Path("/prompt.md"),
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
                    prompt_file=Path("/prompt.md"),
                    mount_path=Path("/workspace"),
                )
            )
        )


def test_fake_agent_runner_pops_responses_in_order():
    r1, r2, r3 = CompletionOutput(), CompletionOutput(), CompletionOutput()
    fake = FakeAgentRunner([r1, r2, r3])
    run = fake.run

    async def _collect():
        p, m = Path("/p.md"), Path("/w")
        return [
            await run(RunRequest(name="A", prompt_file=p, mount_path=m)),
            await run(RunRequest(name="B", prompt_file=p, mount_path=m)),
            await run(RunRequest(name="C", prompt_file=p, mount_path=m)),
        ]

    results = asyncio.run(_collect())
    assert results == [r1, r2, r3]


def test_fake_agent_runner_records_all_calls():
    fake = FakeAgentRunner([CompletionOutput(), CompletionOutput()])
    prompt = Path("/prompt.md")
    mount = Path("/workspace")

    asyncio.run(fake.run(RunRequest(name="X", prompt_file=prompt, mount_path=mount)))
    asyncio.run(fake.run(RunRequest(name="Y", prompt_file=prompt, mount_path=mount)))

    assert len(fake.calls) == 2
    assert fake.calls[0].name == "X"
    assert fake.calls[1].name == "Y"


def test_fake_agent_runner_records_call_kwargs():
    fake = FakeAgentRunner([CompletionOutput()])
    prompt = Path("/prompt.md")
    mount = Path("/workspace")

    asyncio.run(
        fake.run(
            RunRequest(
                name="Planner",
                prompt_file=prompt,
                mount_path=mount,
                prompt_args={"KEY": "val"},
                skip_preflight=True,
                model="claude-3",
                effort="high",
                stage="plan",
            )
        )
    )

    call = fake.calls[0]
    assert call.name == "Planner"
    assert call.prompt_file == prompt
    assert call.mount_path == mount
    assert call.prompt_args == {"KEY": "val"}
    assert call.skip_preflight is True
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
                name="SideEffectAgent", prompt_file=Path("/p.md"), mount_path=Path("/w")
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
                RunRequest(
                    name="Agent", prompt_file=Path("/p.md"), mount_path=Path("/w")
                )
            )
        )


def test_fake_agent_runner_side_effect_still_records_calls():
    async def _effect(request: RunRequest):
        return CompletionOutput()

    fake = FakeAgentRunner(side_effect=_effect)
    asyncio.run(
        fake.run(
            RunRequest(
                name="Recorded", prompt_file=Path("/p.md"), mount_path=Path("/w")
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
            RunRequest(name="Agent", prompt_file=Path("/p.md"), mount_path=Path("/w"))
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
        {}, Config(logs_dir=tmp_path), _make_git_service(), docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
            )
        )
    )

    assert isinstance(result, CompletionOutput)


def test_agent_runner_run_returns_preflight_failure_when_check_fails(tmp_path):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            r = MagicMock()
            r.output = iter([b""])
            return r
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if "ruff check" in cmd:
            return MagicMock(exit_code=1, output=(b"E501 line too long", b""))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    result = asyncio.run(
        runner.run(RunRequest(name="Test", prompt_file=prompt, mount_path=tmp_path))
    )

    assert isinstance(result, PreflightFailure)
    assert len(result.failures) == 1
    name, cmd, output = result.failures[0]
    assert name == "ruff"
    assert "E501" in output


def test_agent_runner_run_skips_preflight_when_skip_preflight_true(tmp_path):
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            r = MagicMock()
            r.output = iter(_COMPLETE_STREAM)
            return r
        cmd = args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
        if "ruff check" in cmd:
            return MagicMock(exit_code=1, output=(b"E501 line too long", b""))
        return MagicMock(exit_code=0, output=(b"", b""))

    mock_container.exec_run.side_effect = exec_side_effect
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
            )
        )
    )

    assert isinstance(result, CompletionOutput)


# ── AgentRunner: error propagation ───────────────────────────────────────────


def test_agent_runner_run_raises_usage_limit_error_when_token_pre_cancelled(tmp_path):
    token = CancellationToken()
    token.cancel()
    mock_client = _make_docker_client([b"output\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), _make_git_service(), docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test", prompt_file=prompt, mount_path=tmp_path, token=token
                )
            )
        )

    mock_client.containers.run.assert_not_called()


def test_agent_runner_run_cancels_token_and_raises_on_usage_limit_in_stream(tmp_path):
    mock_client = _make_docker_client([b"You've hit your session limit\n"])
    token = CancellationToken()
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), _make_git_service(), docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test",
                    prompt_file=prompt,
                    mount_path=tmp_path,
                    skip_preflight=True,
                    token=token,
                )
            )
        )

    assert token.is_cancelled
    assert token.wants_worktree_preserved


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
    cfg = Config(logs_dir=tmp_path, idle_timeout=0.01, timeout_retries=0)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    with pytest.raises(AgentTimeoutError):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test",
                    prompt_file=prompt,
                    mount_path=tmp_path,
                    skip_preflight=True,
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
    cfg = Config(logs_dir=tmp_path, idle_timeout=0.01, timeout_retries=1)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    result = asyncio.run(
        runner.run(
            RunRequest(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
            )
        )
    )

    assert isinstance(result, CompletionOutput)


def test_agent_runner_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with pytest.raises(GitCommandError):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test",
                    prompt_file=prompt,
                    mount_path=tmp_path,
                    skip_preflight=True,
                )
            )
        )


# ── Issue 310: remove_agent lifecycle ────────────────────────────────────────


def test_agent_runner_remove_agent_called_on_success(tmp_path):
    from pycastle.iteration._deps import RecordingStatusDisplay

    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), _make_git_service(), docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run(
            RunRequest(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
                status_display=display,
            )
        )
    )

    assert ("remove", "Test", "finished", "success") in display.calls


def test_agent_runner_remove_agent_called_on_error(tmp_path):
    from pycastle.iteration._deps import RecordingStatusDisplay

    git_svc = _make_git_service()
    git_svc.get_user_name.side_effect = RuntimeError("git failure")
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), git_svc, docker_client=MagicMock()
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")
    display = RecordingStatusDisplay()

    with pytest.raises(RuntimeError, match="git failure"):
        asyncio.run(
            runner.run(
                RunRequest(
                    name="Test",
                    prompt_file=prompt,
                    mount_path=tmp_path,
                    skip_preflight=True,
                    status_display=display,
                )
            )
        )

    assert ("remove", "Test", "finished", "success") in display.calls


# ── AgentRunner: CLAUDE_ACCOUNT_JSON injection ───────────────────────────────


def test_agent_runner_injects_claude_account_json_as_file_not_env(tmp_path):
    mock_client = _make_docker_client(_COMPLETE_STREAM)
    runner = AgentRunner(
        {"CLAUDE_ACCOUNT_JSON": "secret-token", "OTHER": "keep"},
        Config(logs_dir=tmp_path),
        _make_git_service(),
        docker_client=mock_client,
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    asyncio.run(
        runner.run(
            RunRequest(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
            )
        )
    )

    run_call = mock_client.containers.run.call_args
    container_env = run_call.kwargs["environment"]
    assert "CLAUDE_ACCOUNT_JSON" not in container_env
    assert container_env.get("OTHER") == "keep"

    mock_container = mock_client.containers.run.return_value
    mock_container.put_archive.assert_called()
    # First positional arg is the parent dir, second is a tar buffer.
    archive_calls = mock_container.put_archive.call_args_list
    parent_dirs = [c.args[0] for c in archive_calls]
    assert "/home/agent" in parent_dirs


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
    cfg = Config(logs_dir=tmp_path, preflight_checks=())
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


def test_agent_runner_run_preflight_returns_empty_list_when_all_checks_pass(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=0)
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


def test_agent_runner_run_preflight_returns_failure_tuple_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(
        exit_code=1, stdout=b"E501 line too long"
    )
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
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
    cfg = Config(logs_dir=tmp_path)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    mock_client.containers.run.return_value.stop.assert_called()


def test_agent_runner_run_preflight_stops_container_when_check_fails(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"check failed")
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("lint", "lint ."),))
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
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
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
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    result = asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    assert result == []


# ── AgentRunner: run_preflight status_display ────────────────────────────────


def test_agent_runner_run_preflight_registers_and_removes_status_row_on_success(
    tmp_path,
):
    mock_client = _make_preflight_docker_client()
    cfg = Config(logs_dir=tmp_path, preflight_checks=())
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks", mount_path=tmp_path, status_display=display
        )
    )

    assert ("register", "preflight-checks", "started", "Setup") in display.calls
    assert ("remove", "preflight-checks", "finished", "success") in display.calls


def test_agent_runner_run_preflight_updates_phase_for_each_check(tmp_path):
    mock_client = _make_preflight_docker_client()
    checks = (("ruff", "ruff check ."), ("mypy", "mypy ."), ("pytest", "pytest"))
    cfg = Config(logs_dir=tmp_path, preflight_checks=checks)
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run_preflight(
            name="preflight-checks", mount_path=tmp_path, status_display=display
        )
    )

    phase_updates = [c for c in display.calls if c[0] == "update_phase"]
    assert any(c[2] == "Running ruff Checks" for c in phase_updates)
    assert any(c[2] == "Running mypy Checks" for c in phase_updates)
    assert any(c[2] == "Running pytest Checks" for c in phase_updates)


def test_agent_runner_run_preflight_removes_status_row_when_checks_fail(tmp_path):
    mock_client = _make_preflight_docker_client(exit_code=1, stdout=b"E501")
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
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
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)
    display = RecordingStatusDisplay()

    with pytest.raises(RuntimeError, match="unexpected container error"):
        asyncio.run(
            runner.run_preflight(
                name="preflight-checks", mount_path=tmp_path, status_display=display
            )
        )

    assert ("remove", "preflight-checks", "finished", "success") in display.calls


def test_agent_runner_run_preflight_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=MagicMock()
    )

    with pytest.raises(GitCommandError):
        asyncio.run(runner.run_preflight(name="preflight-checks", mount_path=tmp_path))


# ── RunRequest: core interface ────────────────────────────────────────────────


def test_run_request_stores_required_fields():
    from pycastle.agent_output_protocol import AgentRole

    req = RunRequest(
        name="Agent",
        prompt_file=Path("/prompt.md"),
        mount_path=Path("/workspace"),
    )
    assert req.name == "Agent"
    assert req.prompt_file == Path("/prompt.md")
    assert req.mount_path == Path("/workspace")
    assert req.role == AgentRole.IMPLEMENTER
    assert req.prompt_args is None
    assert req.skip_preflight is False
    assert req.model == ""
    assert req.effort == ""
    assert req.stage == ""
    assert req.token is None
    assert req.status_display is None
    assert req.issue_title == ""
    assert req.work_body == ""


def test_fake_agent_runner_accepts_run_request_and_records_it():
    completion = CompletionOutput()
    fake = FakeAgentRunner([completion])
    req = RunRequest(
        name="Planner",
        prompt_file=Path("/p.md"),
        mount_path=Path("/w"),
    )
    result = asyncio.run(fake.run(req))
    assert result is completion
    assert fake.calls[0] is req
