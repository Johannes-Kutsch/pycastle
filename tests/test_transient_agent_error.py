"""Tests for TransientAgentError / HardAgentError three-bucket error classification.

ADR 0023: any is_error: true result envelope from the Claude CLI is classified into
one of three buckets at the parsing layer, never yielded as a successful Result.
"""

from __future__ import annotations

import json

import pytest

from pycastle.agents.output_protocol import AgentRole, process_stream
from pycastle.errors import TransientAgentError


def _5xx_result_line(status: int = 529) -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": True,
            "api_error_status": status,
            "stop_reason": "stop_sequence",
            "result": f"API Error: {status} Overloaded",
        }
    )


# ── First behavior: 5xx result raises TransientAgentError ────────────────────


def test_process_stream_raises_transient_agent_error_on_529_for_reviewer():
    """Exact regression for #831: 529 Overloaded on Reviewer must not succeed."""
    lines = [_5xx_result_line(529)]
    with pytest.raises(TransientAgentError):
        process_stream(lines, on_turn=lambda t: None, role=AgentRole.REVIEWER)


def test_process_stream_raises_transient_agent_error_on_no_status():
    """is_error: true with no api_error_status (network drop / CLI-internal) → TransientAgentError."""
    line = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "internal error: connection reset",
        }
    )
    with pytest.raises(TransientAgentError):
        process_stream([line], on_turn=lambda t: None, role=AgentRole.IMPLEMENTER)


def test_process_stream_raises_transient_agent_error_for_all_5xx_codes():
    """Any 5xx (500, 502, 503, 529) raises TransientAgentError."""
    for status in [500, 502, 503, 529]:
        with pytest.raises(TransientAgentError):
            process_stream(
                [_5xx_result_line(status)],
                on_turn=lambda t: None,
                role=AgentRole.PLANNER,
            )


def test_process_stream_raises_transient_agent_error_for_all_roles():
    """5xx raises TransientAgentError regardless of AgentRole."""
    for role in AgentRole:
        with pytest.raises(TransientAgentError):
            process_stream([_5xx_result_line(529)], on_turn=lambda t: None, role=role)
