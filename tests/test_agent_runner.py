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
from pycastle.git_service import GitService
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

    stream_result = MagicMock()
    stream_result.output = iter(chunks)

    def exec_side_effect(*args, **kwargs):
        if kwargs.get("stream"):
            return stream_result
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
