from pycastle.agents.result import CancellationToken


# ── CancellationToken ─────────────────────────────────────────────────────────


def test_cancellation_token_starts_uncancelled():
    token = CancellationToken()
    assert not token.is_cancelled


def test_cancellation_token_cancel_sets_is_cancelled():
    token = CancellationToken()
    token.cancel()
    assert token.is_cancelled


def test_cancellation_token_second_cancel_is_idempotent():
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.is_cancelled


def test_cancellation_token_constructor_takes_no_arguments():
    token = CancellationToken()
    assert not token.is_cancelled
