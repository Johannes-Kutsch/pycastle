import pytest

from pycastle.services._git_remote_retry import (
    EscalateOperatorActionableGitFailure,
    PassthroughRemoteDivergenceOrConflict,
    RecoverPushNonFastForward,
    RemoteGitRetryPolicy,
    RetryTransientRemoteFailure,
)

_PULL_FETCH_DIVERGENCE_STDERRS = (
    "fatal: Not possible to fast-forward, aborting.",
    "hint: Need to specify how to reconcile divergent branches.",
    "fatal: refusing to merge unrelated histories",
    "CONFLICT (content): Merge conflict in README.md",
)

_PUSH_DIVERGENCE_STDERRS = (
    "hint: Need to specify how to reconcile divergent branches.",
    "fatal: refusing to merge unrelated histories",
    "CONFLICT (content): Merge conflict in README.md",
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


@pytest.mark.parametrize("operation", ["pull", "fetch", "push"])
@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: repository 'git@github.com:owner/repo.git' not found",
        "remote: Repository not found.",
        "fatal: 'origin' does not appear to be a git repository",
        "REMOTE: NOT FOUND",
    ],
)
def test_policy_escalates_stable_remote_misconfig_on_attempt_one(operation, stderr):
    policy = RemoteGitRetryPolicy()

    decision = policy.classify_remote_failure(
        operation=operation,
        stderr=stderr,
        attempt=1,
    )

    assert decision == EscalateOperatorActionableGitFailure()


@pytest.mark.parametrize("operation", ["pull", "fetch"])
@pytest.mark.parametrize("stderr", _PULL_FETCH_DIVERGENCE_STDERRS)
def test_policy_classifies_pull_fetch_divergence_as_passthrough(operation, stderr):
    policy = RemoteGitRetryPolicy()

    decision = policy.classify_remote_failure(
        operation=operation,
        stderr=stderr,
        attempt=1,
    )

    assert decision == PassthroughRemoteDivergenceOrConflict()


@pytest.mark.parametrize("stderr", _PUSH_DIVERGENCE_STDERRS)
def test_policy_classifies_push_divergence_as_passthrough(stderr):
    policy = RemoteGitRetryPolicy()

    decision = policy.classify_remote_failure(
        operation="push",
        stderr=stderr,
        attempt=1,
    )

    assert decision == PassthroughRemoteDivergenceOrConflict()


def test_policy_classifies_push_rejected_stderr_as_named_push_recovery_on_final_attempt():
    policy = RemoteGitRetryPolicy()

    decision = policy.classify_remote_failure(
        operation="push",
        stderr=(
            "! [rejected] main -> main (fetch first)\n"
            "CONFLICT (content): Merge conflict in README.md"
        ),
        attempt=4,
    )

    assert decision == RecoverPushNonFastForward()


@pytest.mark.parametrize("operation", ["pull", "fetch"])
def test_policy_does_not_classify_pull_fetch_rejected_stderr_as_push_recovery(
    operation,
):
    policy = RemoteGitRetryPolicy()

    decision = policy.classify_remote_failure(
        operation=operation,
        stderr="! [rejected] main -> main (fetch first)",
        attempt=1,
    )

    assert decision != RecoverPushNonFastForward()
