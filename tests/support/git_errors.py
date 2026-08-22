"""
Canonical git error instances for use in both real-git contract tests
(tests/test_git_service.py) and test doubles (tests/test_preflight.py,
tests/test_iteration.py).

The real GitService converts raw git stderr into typed exceptions before
any caller sees them; the retry policy escalates transient failures into
OperatorActionableGitError after up to four attempts. Fakes that raise
GitCommandError for operations where the real service raises
OperatorActionableGitError exercise dead code. All fakes should draw from
this module so that adding a new real-git failure state updates both the
contract tests and the fakes in one place, without duplicating the stderr
text.
"""

from pycastle.services import OperatorActionableGitError

# ── Stderr strings exactly as git emits them ──────────────────────────────────

PERMISSION_DENIED_STDERR = "Permission denied (publickey)."
REPO_NOT_FOUND_STDERR = "remote: Repository not found."
CONNECTION_TIMEOUT_STDERR = (
    "ssh: connect to host github.com port 22: Connection timed out"
)

# ── Canonical OperatorActionableGitError instances ────────────────────────────
# These represent failure modes that the real GitService produces for fetch
# operations (including refresh_operating_branch, which uses fetch internally).
# attempt_count=4 means the retry policy exhausted all retries before
# escalating; attempt_count=1 means the failure was deterministically
# operator-actionable on the first attempt.

FETCH_PERMISSION_DENIED = OperatorActionableGitError(
    "git fetch failed",
    stderr=PERMISSION_DENIED_STDERR,
    op="fetch",
    attempt_count=4,
)

FETCH_REPO_NOT_FOUND = OperatorActionableGitError(
    "git fetch failed",
    stderr=REPO_NOT_FOUND_STDERR,
    op="fetch",
    attempt_count=1,
)

FETCH_CONNECTION_TIMEOUT = OperatorActionableGitError(
    "git fetch failed",
    stderr=CONNECTION_TIMEOUT_STDERR,
    op="fetch",
    attempt_count=4,
)
