from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_runtime.errors import HardAgentError

from pycastle.errors import (
    AgentFailedError,
    ModelNotAvailableError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.iteration._implement_dispatch_classify import (
    ClassifyFatal,
    ClassifySuccess,
    classify,
)

_FAKE_PATH = Path("/tmp/fake-worktree")


class _LogCapture:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, Exception]] = []

    def __call__(self, issue: dict, exc: Exception) -> None:
        self.calls.append((issue, exc))


def _issue(n: int) -> dict:
    return {"number": n, "title": f"Issue {n}"}


def test_empty_inputs_returns_success_with_empty_fields() -> None:
    log = _LogCapture()
    out = classify([], [], log_error=log)
    assert isinstance(out, ClassifySuccess)
    assert out.result.completed == []
    assert out.result.errors == []
    assert out.result.usage_limit_hit is False
    assert log.calls == []


def test_all_success_dicts_returns_completed_in_order() -> None:
    issues = [_issue(1), _issue(2), _issue(3)]
    results: list = [{"ok": True}, {"ok": True}, {"ok": True}]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifySuccess)
    assert out.result.completed == issues
    assert out.result.errors == []
    assert out.result.usage_limit_hit is False
    assert log.calls == []


def _make_fatal(fatal_cls: type[Exception]) -> Exception:
    if fatal_cls is AgentFailedError:
        return AgentFailedError("implementer", _FAKE_PATH)
    return fatal_cls("boom")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "fatal_cls",
    [AgentFailedError, HardAgentError, TransientAgentError, ModelNotAvailableError],
)
def test_single_fatal_returns_fatal_variant(fatal_cls: type[Exception]) -> None:
    issues = [_issue(1), _issue(2)]
    fatal = _make_fatal(fatal_cls)
    results: list = [{"ok": True}, fatal]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifyFatal)
    assert out.exception is fatal


def test_fatal_priority_agent_failed_over_hard() -> None:
    issues = [_issue(1), _issue(2)]
    hard = HardAgentError("hard")
    failed = AgentFailedError("implementer", _FAKE_PATH)
    results: list = [hard, failed]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifyFatal)
    assert out.exception is failed


def test_fatal_priority_hard_over_transient() -> None:
    issues = [_issue(1), _issue(2)]
    transient = TransientAgentError("transient")
    hard = HardAgentError("hard")
    results: list = [transient, hard]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifyFatal)
    assert out.exception is hard


def test_fatal_priority_transient_over_model_not_available() -> None:
    issues = [_issue(1), _issue(2)]
    mna = ModelNotAvailableError("mna")
    transient = TransientAgentError("transient")
    results: list = [mna, transient]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifyFatal)
    assert out.exception is transient


def test_fatal_priority_all_four_classes() -> None:
    issues = [_issue(1), _issue(2), _issue(3), _issue(4)]
    mna = ModelNotAvailableError("mna")
    transient = TransientAgentError("transient")
    hard = HardAgentError("hard")
    failed = AgentFailedError("implementer", _FAKE_PATH)
    results: list = [mna, transient, hard, failed]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifyFatal)
    assert out.exception is failed


def test_single_usage_limit_populates_six_fields() -> None:
    issues = [_issue(1)]
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    err = UsageLimitError(
        reset_time=ts,
        raw_message="limit reached",
        provider="anthropic",
        is_permanent=False,
        account_label="acct-1",
    )
    results: list = [err]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifySuccess)
    r = out.result
    assert r.usage_limit_hit is True
    assert r.usage_limit_reset_time == ts
    assert r.usage_limit_provider == "anthropic"
    assert r.usage_limit_raw_message == "limit reached"
    assert r.usage_limit_account_label == "acct-1"
    assert r.usage_limit_is_permanent is False
    assert r.completed == []
    assert r.errors == []
    assert log.calls == []


def test_usage_limit_excluded_from_completed_and_errors() -> None:
    issues = [_issue(1), _issue(2), _issue(3)]
    err = UsageLimitError()
    results: list = [{"ok": True}, err, {"ok": True}]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifySuccess)
    assert out.result.completed == [_issue(1), _issue(3)]
    assert out.result.errors == []
    assert log.calls == []


def test_multiple_usage_limit_reset_time_first_with_value() -> None:
    issues = [_issue(1), _issue(2), _issue(3)]
    ts = datetime(2026, 6, 1, tzinfo=UTC)
    err_no_time = UsageLimitError(reset_time=None, raw_message="first", provider="p1")
    err_with_time = UsageLimitError(reset_time=ts, raw_message="second", provider="p2")
    err_also_time = UsageLimitError(
        reset_time=datetime(2027, 1, 1, tzinfo=UTC),
        raw_message="third",
        provider="p3",
    )
    results: list = [err_no_time, err_with_time, err_also_time]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifySuccess)
    r = out.result
    assert r.usage_limit_hit is True
    assert r.usage_limit_reset_time == ts
    assert r.usage_limit_provider == "p1"
    assert r.usage_limit_raw_message == "first"


def test_non_fatal_exceptions_go_to_errors_and_logger() -> None:
    issues = [_issue(1), _issue(2), _issue(3)]
    nonfatal1 = ValueError("bad value")
    nonfatal2 = RuntimeError("runtime")
    results: list = [{"ok": True}, nonfatal1, nonfatal2]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifySuccess)
    assert out.result.completed == [_issue(1)]
    assert out.result.errors == [(issues[1], nonfatal1), (issues[2], nonfatal2)]
    assert log.calls == [(issues[1], nonfatal1), (issues[2], nonfatal2)]


def test_non_fatal_logger_called_in_input_order() -> None:
    issues = [_issue(1), _issue(2), _issue(3), _issue(4)]
    e1 = ValueError("e1")
    e2 = OSError("e2")
    e3 = KeyError("e3")
    results: list = [e3, {"ok": True}, e1, e2]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifySuccess)
    assert [exc for _, exc in log.calls] == [e3, e1, e2]


def test_fatal_wins_over_usage_limit() -> None:
    issues = [_issue(1), _issue(2)]
    usage = UsageLimitError()
    fatal = AgentFailedError("implementer", _FAKE_PATH)
    results: list = [usage, fatal]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifyFatal)
    assert out.exception is fatal


def test_fatal_wins_over_usage_limit_and_nonfatal() -> None:
    issues = [_issue(1), _issue(2), _issue(3)]
    usage = UsageLimitError()
    nonfatal = ValueError("nonfatal")
    fatal = HardAgentError("hard")
    results: list = [usage, nonfatal, fatal]
    log = _LogCapture()
    out = classify(issues, results, log_error=log)
    assert isinstance(out, ClassifyFatal)
    assert out.exception is fatal
