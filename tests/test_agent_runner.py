"""Tests for AgentRunner and FakeAgentRunner."""

import asyncio
from pathlib import Path

import pytest

from pycastle.agent_result import PreflightFailure
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
