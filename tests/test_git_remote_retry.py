import pytest

from pycastle.services._git_remote_retry import (
    EscalateOperatorActionableGitFailure,
    RemoteGitRetryPolicy,
    RetryTransientRemoteFailure,
)


@pytest.mark.parametrize("operation", ["pull", "fetch", "push"])
@pytest.mark.parametrize(
    ("attempt", "delay_seconds"),
    [(1, 10), (2, 60), (3, 300)],
)
def test_policy_retries_unclassified_transient_remote_failures_with_adr_0026_backoff(
    operation, attempt, delay_seconds
):
    policy = RemoteGitRetryPolicy()

    decision = policy.classify_remote_failure(
        operation=operation,
        stderr="fatal: unable to access remote: temporary network flap",
        attempt=attempt,
    )

    assert decision == RetryTransientRemoteFailure(delay_seconds=delay_seconds)


@pytest.mark.parametrize("operation", ["pull", "fetch", "push"])
def test_policy_escalates_unclassified_transient_remote_failures_on_attempt_four(
    operation,
):
    policy = RemoteGitRetryPolicy()

    decision = policy.classify_remote_failure(
        operation=operation,
        stderr="fatal: unable to access remote: temporary network flap",
        attempt=4,
    )

    assert decision == EscalateOperatorActionableGitFailure()
