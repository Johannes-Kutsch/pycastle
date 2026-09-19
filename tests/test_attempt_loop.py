from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_runtime.errors import ProviderUnavailableReason
from agent_runtime.runtime import (
    Cancelled,
    Completed,
    ModelNotAvailable,
    ProviderUnavailable,
    TimedOut,
    UsageLimited,
)

from pycastle.agents.attempt_loop import (
    _MAX_PROTOCOL_RETRIES,
    _decide_transition,
    _RaiseAgentFailed,
    _RaiseModelNotAvailable,
    _RaiseProviderUsageLimit,
    _RaiseTimeout,
    _RaiseTransientError,
    _RaiseUsageLimit,
    _Reprompt,
    _ResumeAfterTimeout,
    _ReturnCancelled,
    _ReturnParsed,
)
from pycastle.agents.output_protocol import AgentRole
from pycastle.agents.protocol_reprompt import (
    GENERIC_PROTOCOL_REPROMPT_MESSAGE,
    GenericProtocolReprompt,
    TemplateSpecificProtocolReprompt,
    UnsupportedProtocolReprompt,
)

_FAKE_SELECTED = SimpleNamespace(service="codex", model="gpt-5.5")
_FAKE_MOUNT = Path("/fake/wt")
_FAKE_SESSION_STORE = Path("/fake/session")

_VALID_IMPLEMENTER_OUTPUT = "<commit_message>done</commit_message>"
_VALID_PLANNER_OUTPUT = '<plan>{"issues": []}</plan>'
_INVALID_PLANNER_OUTPUT = "no output at all"


def _call(outcome_kind, **overrides):
    """Call _decide_transition with sensible defaults."""
    kwargs = {
        "attempt": 0,
        "retries_left": 2,
        "timeout_retries": 2,
        "selected": _FAKE_SELECTED,
        "output_text": _VALID_IMPLEMENTER_OUTPUT,
        "role": AgentRole.IMPLEMENTER,
        "protocol_reprompt_plan": lambda _: UnsupportedProtocolReprompt(),
        "preserve_session_on_completion": False,
        "role_value": "implementer",
        "mount_path": _FAKE_MOUNT,
        "session_namespace": "",
        "service_name": "codex",
        "session_store": _FAKE_SESSION_STORE,
        "log_path": None,
        "stage_key": "implement",
    }
    kwargs.update(overrides)
    return _decide_transition(outcome_kind, **kwargs)


# ---------------------------------------------------------------------------
# Cancelled
# ---------------------------------------------------------------------------


def test_cancelled_returns_cancelled_directive():
    d = _call(Cancelled())
    assert isinstance(d, _ReturnCancelled)


# ---------------------------------------------------------------------------
# Completed — success
# ---------------------------------------------------------------------------


def test_completed_success_returns_parsed():
    d = _call(Completed(), output_text=_VALID_IMPLEMENTER_OUTPUT)
    assert isinstance(d, _ReturnParsed)
    assert d.parsed is not None


def test_completed_success_clears_when_preserve_false():
    d = _call(
        Completed(),
        output_text=_VALID_IMPLEMENTER_OUTPUT,
        preserve_session_on_completion=False,
    )
    assert isinstance(d, _ReturnParsed)
    assert d.clear_completion is True


def test_completed_success_keeps_when_preserve_true():
    d = _call(
        Completed(),
        output_text=_VALID_IMPLEMENTER_OUTPUT,
        preserve_session_on_completion=True,
    )
    assert isinstance(d, _ReturnParsed)
    assert d.clear_completion is False


# ---------------------------------------------------------------------------
# Completed — parse error below cap → reprompt
# ---------------------------------------------------------------------------


def _call_planner(**overrides):
    kwargs = {"role": AgentRole.PLANNER, "output_text": _INVALID_PLANNER_OUTPUT}
    kwargs.update(overrides)
    return _call(Completed(), **kwargs)


def test_completed_parse_error_below_cap_returns_reprompt():
    d = _call_planner(attempt=0)
    assert isinstance(d, _Reprompt)


def test_completed_parse_error_reprompt_uses_generic_when_unsupported():
    d = _call_planner(
        attempt=0,
        protocol_reprompt_plan=lambda _: UnsupportedProtocolReprompt(),
    )
    assert isinstance(d, _Reprompt)
    assert d.message == GENERIC_PROTOCOL_REPROMPT_MESSAGE


def test_completed_parse_error_reprompt_uses_plan_message():
    custom = "Custom reprompt message"
    d = _call_planner(
        attempt=0,
        protocol_reprompt_plan=lambda _: TemplateSpecificProtocolReprompt(
            message=custom
        ),
    )
    assert isinstance(d, _Reprompt)
    assert d.message == custom


def test_completed_parse_error_reprompt_uses_generic_plan_message():
    d = _call_planner(
        attempt=0,
        protocol_reprompt_plan=lambda _: GenericProtocolReprompt(),
    )
    assert isinstance(d, _Reprompt)
    assert d.message == GENERIC_PROTOCOL_REPROMPT_MESSAGE


# ---------------------------------------------------------------------------
# Completed — parse error at cap → agent failed
# ---------------------------------------------------------------------------


def test_completed_parse_error_at_cap_raises_agent_failed():
    d = _call_planner(attempt=_MAX_PROTOCOL_RETRIES)
    assert isinstance(d, _RaiseAgentFailed)


def test_completed_parse_error_at_cap_carries_context():
    log = Path("/logs/agent.log")
    d = _call_planner(
        attempt=_MAX_PROTOCOL_RETRIES,
        role_value="planner",
        mount_path=_FAKE_MOUNT,
        session_namespace="ns1",
        service_name="claude",
        session_store=_FAKE_SESSION_STORE,
        log_path=log,
    )
    assert isinstance(d, _RaiseAgentFailed)
    assert d.role_value == "planner"
    assert d.mount_path == _FAKE_MOUNT
    assert d.session_namespace == "ns1"
    assert d.service_name == "claude"
    assert d.session_store == _FAKE_SESSION_STORE
    assert d.log_path == log


# ---------------------------------------------------------------------------
# UsageLimited — permanent vs non-permanent
# ---------------------------------------------------------------------------


def test_usage_limited_non_permanent_returns_raise_usage_limit():
    d = _call(UsageLimited(reset_time=None, is_permanent=False))
    assert isinstance(d, _RaiseUsageLimit)
    assert d.is_permanent is False


def test_usage_limited_permanent_returns_raise_usage_limit():
    d = _call(UsageLimited(reset_time=None, is_permanent=True))
    assert isinstance(d, _RaiseUsageLimit)
    assert d.is_permanent is True


def test_usage_limited_carries_reset_time():
    from datetime import UTC, datetime

    t = datetime(2025, 1, 1, tzinfo=UTC)
    d = _call(UsageLimited(reset_time=t, is_permanent=False))
    assert isinstance(d, _RaiseUsageLimit)
    assert d.reset_time == t


def test_usage_limited_carries_provider_from_selected():
    d = _call(
        UsageLimited(reset_time=None, is_permanent=False),
        selected=SimpleNamespace(service="claude", model="sonnet"),
    )
    assert isinstance(d, _RaiseUsageLimit)
    assert d.provider == "claude"


# ---------------------------------------------------------------------------
# ProviderUnavailable — transient vs non-transient
# ---------------------------------------------------------------------------


def test_provider_unavailable_transient_returns_transient_error():
    d = _call(
        ProviderUnavailable(
            reason=ProviderUnavailableReason.TRANSIENT_API_ERROR, detail="oops"
        )
    )
    assert isinstance(d, _RaiseTransientError)
    assert d.detail == "oops"


def test_provider_unavailable_non_transient_returns_provider_usage_limit():
    non_transient = next(
        r
        for r in ProviderUnavailableReason
        if r is not ProviderUnavailableReason.TRANSIENT_API_ERROR
    )
    d = _call(ProviderUnavailable(reason=non_transient, detail="rate limit"))
    assert isinstance(d, _RaiseProviderUsageLimit)
    assert d.raw_message == "rate limit"


def test_provider_unavailable_non_transient_carries_provider():
    non_transient = next(
        r
        for r in ProviderUnavailableReason
        if r is not ProviderUnavailableReason.TRANSIENT_API_ERROR
    )
    d = _call(
        ProviderUnavailable(reason=non_transient, detail="rate limit"),
        selected=SimpleNamespace(service="codex", model="gpt-5.5"),
    )
    assert isinstance(d, _RaiseProviderUsageLimit)
    assert d.provider == "codex"


# ---------------------------------------------------------------------------
# TimedOut — retries remaining vs exhausted
# ---------------------------------------------------------------------------


def test_timed_out_with_retries_remaining_returns_resume():
    d = _call(TimedOut(), retries_left=2, timeout_retries=3)
    assert isinstance(d, _ResumeAfterTimeout)


def test_timed_out_restart_num_computed_correctly():
    d = _call(TimedOut(), retries_left=2, timeout_retries=3)
    assert isinstance(d, _ResumeAfterTimeout)
    assert d.restart_num == 2  # timeout_retries - retries_left + 1 = 3 - 2 + 1


def test_timed_out_first_retry_restart_num():
    d = _call(TimedOut(), retries_left=3, timeout_retries=3)
    assert isinstance(d, _ResumeAfterTimeout)
    assert d.restart_num == 1


def test_timed_out_no_retries_returns_raise_timeout():
    d = _call(TimedOut(), retries_left=0)
    assert isinstance(d, _RaiseTimeout)


def test_timed_out_exhausted_carries_role_value():
    d = _call(TimedOut(), retries_left=0, role_value="planner")
    assert isinstance(d, _RaiseTimeout)
    assert d.role_value == "planner"


# ---------------------------------------------------------------------------
# ModelNotAvailable
# ---------------------------------------------------------------------------


def test_model_not_available_returns_raise_model_not_available():
    d = _call(
        ModelNotAvailable(),
        selected=SimpleNamespace(service="claude", model="opus"),
        stage_key="plan",
    )
    assert isinstance(d, _RaiseModelNotAvailable)
    assert d.service == "claude"
    assert d.model == "opus"
    assert d.stage_key == "plan"
