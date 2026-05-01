"""Tests for AgentRunner and FakeAgentRunner."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.agent_result import CancellationToken, PreflightFailure
from pycastle.agent_runner import AgentRunner
from pycastle.config import Config
from pycastle.errors import AgentTimeoutError, BranchCollisionError, UsageLimitError
from pycastle.git_service import GitCommandError, GitService
from pycastle.iteration._deps import FakeAgentRunner


# ── FakeAgentRunner: queue behaviour ─────────────────────────────────────────


def test_fake_agent_runner_returns_queued_string_response():
    fake = FakeAgentRunner(["<promise>COMPLETE</promise>"])
    result = asyncio.run(
        fake.run(
            name="Tester",
            prompt_file=Path("/prompt.md"),
            mount_path=Path("/workspace"),
        )
    )
    assert result == "<promise>COMPLETE</promise>"


def test_fake_agent_runner_returns_queued_preflight_failure():
    failure = PreflightFailure(failures=(("ruff", "ruff check .", "E501"),))
    fake = FakeAgentRunner([failure])
    result = asyncio.run(
        fake.run(
            name="Tester",
            prompt_file=Path("/prompt.md"),
            mount_path=Path("/workspace"),
        )
    )
    assert result is failure


def test_fake_agent_runner_raises_queued_exception():
    fake = FakeAgentRunner([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            fake.run(
                name="Tester",
                prompt_file=Path("/prompt.md"),
                mount_path=Path("/workspace"),
            )
        )


def test_fake_agent_runner_raises_assertion_error_when_queue_exhausted():
    fake = FakeAgentRunner([])
    with pytest.raises(AssertionError, match="queue exhausted"):
        asyncio.run(
            fake.run(
                name="Unexpected",
                prompt_file=Path("/prompt.md"),
                mount_path=Path("/workspace"),
            )
        )


def test_fake_agent_runner_exhaustion_error_includes_agent_name():
    fake = FakeAgentRunner([])
    with pytest.raises(AssertionError, match="MyAgent"):
        asyncio.run(
            fake.run(
                name="MyAgent",
                prompt_file=Path("/prompt.md"),
                mount_path=Path("/workspace"),
            )
        )


def test_fake_agent_runner_pops_responses_in_order():
    fake = FakeAgentRunner(["first", "second", "third"])
    run = fake.run

    async def _collect():
        kwargs = {"prompt_file": Path("/p.md"), "mount_path": Path("/w")}
        return [
            await run(name="A", **kwargs),
            await run(name="B", **kwargs),
            await run(name="C", **kwargs),
        ]

    results = asyncio.run(_collect())
    assert results == ["first", "second", "third"]


def test_fake_agent_runner_records_all_calls():
    fake = FakeAgentRunner(["a", "b"])
    prompt = Path("/prompt.md")
    mount = Path("/workspace")

    asyncio.run(fake.run(name="X", prompt_file=prompt, mount_path=mount))
    asyncio.run(fake.run(name="Y", prompt_file=prompt, mount_path=mount))

    assert len(fake.calls) == 2
    assert fake.calls[0]["name"] == "X"
    assert fake.calls[1]["name"] == "Y"


def test_fake_agent_runner_records_call_kwargs():
    fake = FakeAgentRunner(["ok"])
    prompt = Path("/prompt.md")
    mount = Path("/workspace")

    asyncio.run(
        fake.run(
            name="Planner",
            prompt_file=prompt,
            mount_path=mount,
            prompt_args={"KEY": "val"},
            branch="my-branch",
            sha="abc123",
            skip_preflight=True,
            model="claude-3",
            effort="high",
            stage="plan",
        )
    )

    call = fake.calls[0]
    assert call["name"] == "Planner"
    assert call["prompt_file"] == prompt
    assert call["mount_path"] == mount
    assert call["prompt_args"] == {"KEY": "val"}
    assert call["branch"] == "my-branch"
    assert call["sha"] == "abc123"
    assert call["skip_preflight"] is True
    assert call["model"] == "claude-3"
    assert call["effort"] == "high"
    assert call["stage"] == "plan"


def test_fake_agent_runner_starts_with_empty_calls():
    fake = FakeAgentRunner(["ok"])
    assert fake.calls == []


# ── FakeAgentRunner: side_effect mode ────────────────────────────────────────


def test_fake_agent_runner_side_effect_is_called_with_kwargs():
    received: dict = {}

    async def _effect(name, **kwargs):
        received["name"] = name
        return "from side effect"

    fake = FakeAgentRunner(side_effect=_effect)
    result = asyncio.run(
        fake.run(
            name="SideEffectAgent",
            prompt_file=Path("/p.md"),
            mount_path=Path("/w"),
        )
    )

    assert result == "from side effect"
    assert received["name"] == "SideEffectAgent"


def test_fake_agent_runner_side_effect_can_raise():
    async def _effect(**kwargs):
        raise ValueError("side effect error")

    fake = FakeAgentRunner(side_effect=_effect)
    with pytest.raises(ValueError, match="side effect error"):
        asyncio.run(
            fake.run(
                name="Agent",
                prompt_file=Path("/p.md"),
                mount_path=Path("/w"),
            )
        )


def test_fake_agent_runner_side_effect_still_records_calls():
    async def _effect(**kwargs):
        return "ok"

    fake = FakeAgentRunner(side_effect=_effect)
    asyncio.run(
        fake.run(
            name="Recorded",
            prompt_file=Path("/p.md"),
            mount_path=Path("/w"),
        )
    )

    assert len(fake.calls) == 1
    assert fake.calls[0]["name"] == "Recorded"


def test_fake_agent_runner_side_effect_can_be_synchronous():
    def _sync_effect(**kwargs):
        return "sync result"

    fake = FakeAgentRunner(side_effect=_sync_effect)
    result = asyncio.run(
        fake.run(
            name="Agent",
            prompt_file=Path("/p.md"),
            mount_path=Path("/w"),
        )
    )

    assert result == "sync result"


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
    mock_client = _make_docker_client([b"agent output\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), _make_git_service(), docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    result = asyncio.run(
        runner.run(
            name="Test", prompt_file=prompt, mount_path=tmp_path, skip_preflight=True
        )
    )

    assert result == "agent output\n"


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
        runner.run(name="Test", prompt_file=prompt, mount_path=tmp_path)
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
            r.output = iter([b"done\n"])
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
            name="Test", prompt_file=prompt, mount_path=tmp_path, skip_preflight=True
        )
    )

    assert isinstance(result, str)


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
                name="Test", prompt_file=prompt, mount_path=tmp_path, token=token
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
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
                token=token,
            )
        )

    assert token.is_cancelled
    assert token.wants_worktree_preserved


def test_agent_runner_run_raises_branch_collision_for_concurrent_same_branch(tmp_path):
    mock_client = _make_docker_client([b"output\n"])
    mock_git = _make_git_service()
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")

    async def _two_on_same_branch():
        return await asyncio.gather(
            runner.run(
                name="A1",
                prompt_file=prompt,
                mount_path=tmp_path,
                branch="feature/collision",
                skip_preflight=True,
            ),
            runner.run(
                name="A2",
                prompt_file=prompt,
                mount_path=tmp_path,
                branch="feature/collision",
                skip_preflight=True,
            ),
            return_exceptions=True,
        )

    results = asyncio.run(_two_on_same_branch())
    errors = [r for r in results if isinstance(r, Exception)]
    assert any(isinstance(e, BranchCollisionError) for e in errors)


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
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
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
                _never_yields() if stream_call_count["n"] == 1 else iter([b"done\n"])
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
            name="Test", prompt_file=prompt, mount_path=tmp_path, skip_preflight=True
        )
    )

    assert result == "done\n"


# ── AgentRunner: worktree lifecycle ──────────────────────────────────────────


def test_agent_runner_creates_worktree_at_issue_path(tmp_path):
    mock_git = _make_git_service()
    mock_client = _make_docker_client([b"done\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    asyncio.run(
        runner.run(
            name="Test",
            prompt_file=prompt,
            mount_path=tmp_path,
            branch="pycastle/issue-42",
            skip_preflight=True,
        )
    )

    worktree_path = mock_git.create_worktree.call_args[0][1]
    assert worktree_path.name == "issue-42"


def test_agent_runner_sanitizes_branch_name_for_worktree_path(tmp_path):
    mock_git = _make_git_service()
    mock_client = _make_docker_client([b"done\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    asyncio.run(
        runner.run(
            name="Test",
            prompt_file=prompt,
            mount_path=tmp_path,
            branch="feature/My Cool Branch",
            skip_preflight=True,
        )
    )

    worktree_path = mock_git.create_worktree.call_args[0][1]
    assert worktree_path.name == "feature-my-cool-branch"


def test_agent_runner_removes_worktree_when_clean(tmp_path):
    mock_git = _make_git_service()
    mock_git.is_working_tree_clean.return_value = True
    mock_client = _make_docker_client([b"done\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    asyncio.run(
        runner.run(
            name="Test",
            prompt_file=prompt,
            mount_path=tmp_path,
            branch="feature/test",
            skip_preflight=True,
        )
    )

    mock_git.remove_worktree.assert_called_once()


def test_agent_runner_preserves_worktree_when_dirty(tmp_path):
    mock_git = _make_git_service()
    mock_git.is_working_tree_clean.return_value = False
    mock_client = _make_docker_client([b"done\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    asyncio.run(
        runner.run(
            name="Test",
            prompt_file=prompt,
            mount_path=tmp_path,
            branch="feature/test",
            skip_preflight=True,
        )
    )

    mock_git.remove_worktree.assert_not_called()


def test_agent_runner_preserves_worktree_on_usage_limit(tmp_path):
    mock_git = _make_git_service()
    mock_client = _make_docker_client([b"You've hit your session limit\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with pytest.raises(UsageLimitError):
        asyncio.run(
            runner.run(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                branch="feature/test",
                skip_preflight=True,
            )
        )

    mock_git.remove_worktree.assert_not_called()


def test_agent_runner_does_not_start_container_when_create_worktree_fails(tmp_path):
    mock_git = _make_git_service()
    mock_git.create_worktree.side_effect = RuntimeError("git worktree add failed")
    mock_client = _make_docker_client([b"done\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with pytest.raises(RuntimeError, match="worktree add failed"):
        asyncio.run(
            runner.run(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                branch="feature/test",
                skip_preflight=True,
            )
        )

    mock_client.containers.run.assert_not_called()


def test_agent_runner_propagates_git_user_name_error(tmp_path):
    mock_git = _make_git_service()
    mock_git.get_user_name.side_effect = GitCommandError("git config user.name failed")
    mock_client = _make_docker_client([b"done\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), mock_git, docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("test")

    with pytest.raises(GitCommandError):
        asyncio.run(
            runner.run(
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
            )
        )


# ── Issue 310: remove_agent lifecycle ────────────────────────────────────────


def test_agent_runner_remove_agent_called_on_success(tmp_path):
    from pycastle.iteration._deps import RecordingStatusDisplay

    mock_client = _make_docker_client([b"done\n"])
    runner = AgentRunner(
        {}, Config(logs_dir=tmp_path), _make_git_service(), docker_client=mock_client
    )
    prompt = tmp_path / "p.md"
    prompt.write_text("Test prompt")
    display = RecordingStatusDisplay()

    asyncio.run(
        runner.run(
            name="Test",
            prompt_file=prompt,
            mount_path=tmp_path,
            skip_preflight=True,
            status_display=display,
        )
    )

    assert ("remove_agent", "Test") in display.calls


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
                name="Test",
                prompt_file=prompt,
                mount_path=tmp_path,
                skip_preflight=True,
                status_display=display,
            )
        )

    assert ("remove_agent", "Test") in display.calls


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


def test_agent_runner_run_preflight_installs_dependencies_before_running_checks(
    tmp_path,
):
    mock_client = _make_preflight_docker_client()
    cfg = Config(logs_dir=tmp_path, preflight_checks=(("ruff", "ruff check ."),))
    runner = AgentRunner({}, cfg, _make_git_service(), docker_client=mock_client)

    asyncio.run(runner.run_preflight(name="plan-sandbox", mount_path=tmp_path))

    all_commands = [
        call.args[0]
        for call in mock_client.containers.run.return_value.exec_run.call_args_list
    ]
    flat_commands = [" ".join(cmd) for cmd in all_commands]
    pip_indices = [i for i, c in enumerate(flat_commands) if "pip install" in c]
    ruff_indices = [i for i, c in enumerate(flat_commands) if "ruff check" in c]
    assert pip_indices, "pip install must be called during run_preflight"
    assert ruff_indices, "preflight check must be called"
    assert pip_indices[-1] < ruff_indices[0], (
        "pip install must run before preflight checks"
    )
