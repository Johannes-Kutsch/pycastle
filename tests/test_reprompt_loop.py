"""Tests for the re-prompt loop module."""

import asyncio


from pycastle.agents.output_protocol import (
    CommitMessageOutput,
    CompletionOutput,
    FailedOutput,
    PlanParseError,
    PromiseParseError,
)
from pycastle.iteration.reprompt_loop import run_with_reprompt


def _sequence(*items):
    """Factory that returns items in order; raises if item is an Exception."""
    it = iter(items)

    async def factory(msg):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return factory


# ── Valid output on first turn ────────────────────────────────────────────────


def test_returns_output_on_first_turn():
    output = CommitMessageOutput(message="done")
    result = asyncio.run(
        run_with_reprompt(_sequence(output), reprompt_message="try again")
    )
    assert result == output


# ── Valid output after one retry ──────────────────────────────────────────────


def test_returns_output_after_one_retry():
    output = CompletionOutput()
    result = asyncio.run(
        run_with_reprompt(
            _sequence(PromiseParseError("no tag"), output),
            reprompt_message="try again",
        )
    )
    assert result == output


# ── FAILED on first turn ──────────────────────────────────────────────────────


def test_returns_failed_output_when_agent_signals_failed():
    result = asyncio.run(
        run_with_reprompt(_sequence(FailedOutput()), reprompt_message="try again")
    )
    assert isinstance(result, FailedOutput)


# ── FAILED synthesized after exhausting budget ────────────────────────────────


def test_synthesizes_failed_output_after_exhausting_default_budget():
    result = asyncio.run(
        run_with_reprompt(
            _sequence(
                PromiseParseError("no tag"),
                PromiseParseError("no tag"),
                PromiseParseError("no tag"),
            ),
            reprompt_message="try again",
        )
    )
    assert isinstance(result, FailedOutput)


def test_default_budget_is_three_attempts():
    call_count = 0

    async def counting_factory(msg):
        nonlocal call_count
        call_count += 1
        raise PromiseParseError("no tag")

    asyncio.run(run_with_reprompt(counting_factory, reprompt_message="try again"))
    assert call_count == 3


# ── malformed-then-valid recovery ────────────────────────────────────────────


def test_recovers_from_malformed_output_to_valid():
    output = CommitMessageOutput(message="fixed")
    result = asyncio.run(
        run_with_reprompt(
            _sequence(PlanParseError("bad json"), output),
            reprompt_message="try again",
        )
    )
    assert result == output


# ── budget parameter ─────────────────────────────────────────────────────────


def test_custom_budget_one_synthesizes_failed_immediately():
    result = asyncio.run(
        run_with_reprompt(
            _sequence(PromiseParseError("no tag")),
            reprompt_message="try again",
            budget=1,
        )
    )
    assert isinstance(result, FailedOutput)


def test_custom_budget_two_allows_one_retry():
    output = CompletionOutput()
    result = asyncio.run(
        run_with_reprompt(
            _sequence(PromiseParseError("first fails"), output),
            reprompt_message="try again",
            budget=2,
        )
    )
    assert result == output


# ── reprompt message is passed on retries ─────────────────────────────────────


def test_initial_attempt_receives_none():
    received: list[str | None] = []

    async def recording_factory(msg):
        received.append(msg)
        raise PromiseParseError("always fails")

    asyncio.run(
        run_with_reprompt(recording_factory, reprompt_message="please retry", budget=1)
    )
    assert received == [None]


def test_retry_attempts_receive_reprompt_message():
    received: list[str | None] = []
    output = CompletionOutput()

    async def recording_factory(msg):
        received.append(msg)
        if msg is None:
            raise PromiseParseError("first attempt fails")
        return output

    asyncio.run(run_with_reprompt(recording_factory, reprompt_message="please retry"))
    assert received[0] is None
    assert received[1] == "please retry"
