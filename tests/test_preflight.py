"""Tests for PreflightCache.get_safe_sha: observable behaviour via the public interface."""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from unittest.mock import MagicMock

import shutil

from pycastle.agents.output_protocol import CompletionOutput, IssueOutput
from pycastle.config import Config
from pycastle.services import GitCommandError, GitService, GithubService
from pycastle.iteration._deps import FakeAgentRunner
from pycastle.display.status_display import PlainStatusDisplay
from pycastle.iteration.preflight import (
    PreflightAFK,
    PreflightCache,
    PreflightHITL,
    PreflightReady,
)


@dataclasses.dataclass
class _CacheDeps:
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: PlainStatusDisplay
    agent_runner: FakeAgentRunner
    repo_root: Path


@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    return svc


@pytest.fixture
def github_svc():
    return MagicMock(spec=GithubService)


def _make_deps(tmp_path, agent_runner, *, git_svc, github_svc):
    return _CacheDeps(
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=agent_runner,
        cfg=Config(max_parallel=4, max_iterations=1),
        status_display=PlainStatusDisplay(),
    )


# ── get_safe_sha: basic return variants ──────────────────────────────────────


def test_get_safe_sha_returns_ready_with_sha_when_checks_pass(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"


def test_get_safe_sha_returns_hitl_when_checks_fail_with_hitl_label(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 55
    assert result.sha == "abc123"


def test_get_safe_sha_returns_afk_when_checks_fail_with_afk_label(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=42, labels=["bug", "ready-for-agent", "behavior-slice"])],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightAFK)
    assert result.issue_number == 42
    assert result.sha == "abc123"


# ── get_safe_sha: same-SHA cache hit ─────────────────────────────────────────


def test_get_safe_sha_returns_cached_verdict_on_same_sha_second_call(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result1 = asyncio.run(cache.get_safe_sha(deps))
    result2 = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result1, PreflightReady)
    assert result2 is result1
    assert len(fake.preflight_calls) == 1


def test_get_safe_sha_failure_cached_on_second_call_at_same_sha(
    tmp_path, git_svc, github_svc
):
    """Cache miss + checks fail dispatches preflight-issue once; second call at same
    SHA reuses the cached AFK verdict without re-running checks or re-filing."""
    fake = FakeAgentRunner(
        [IssueOutput(number=99, labels=["ready-for-agent", "refactor-slice"])],
        preflight_responses=[[("mypy", "mypy .", "error")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result1 = asyncio.run(cache.get_safe_sha(deps))
    result2 = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result1, PreflightAFK)
    assert result1.issue_number == 99
    assert result2 is result1
    assert len(fake.preflight_calls) == 1
    assert len(fake.calls) == 1  # preflight-issue agent called only once


# ── get_safe_sha: HEAD advance replaces slot ──────────────────────────────────


def test_get_safe_sha_reruns_checks_when_head_advances(tmp_path, git_svc, github_svc):
    git_svc.get_head_sha.side_effect = ["sha-v1", "sha-v2"]
    fake = FakeAgentRunner([], preflight_responses=[[], []])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result1 = asyncio.run(cache.get_safe_sha(deps))
    result2 = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result1, PreflightReady)
    assert result1.sha == "sha-v1"
    assert isinstance(result2, PreflightReady)
    assert result2.sha == "sha-v2"
    assert len(fake.preflight_calls) == 2


# ── get_safe_sha: pull failure ────────────────────────────────────────────────


def test_get_safe_sha_propagates_git_command_error_on_pull_failure(
    tmp_path, git_svc, github_svc
):
    git_svc.pull_with_merge_fallback.side_effect = GitCommandError(
        "git pull --ff-only failed"
    )
    fake = FakeAgentRunner([], preflight_responses=[])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(GitCommandError):
        asyncio.run(cache.get_safe_sha(deps))

    git_svc.get_head_sha.assert_not_called()


def test_get_safe_sha_leaves_slot_unchanged_on_pull_failure(
    tmp_path, git_svc, github_svc
):
    """Pull failure must not corrupt the cache slot."""
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    # First call succeeds
    result1 = asyncio.run(cache.get_safe_sha(deps))
    assert isinstance(result1, PreflightReady)

    # Second call: pull fails
    git_svc.pull_with_merge_fallback.side_effect = GitCommandError("diverged")
    # Different SHA so it would invalidate cache
    git_svc.get_head_sha.return_value = "sha-new"

    with pytest.raises(GitCommandError):
        asyncio.run(cache.get_safe_sha(deps))

    # The slot still holds the original verdict
    git_svc.pull_with_merge_fallback.side_effect = None
    git_svc.get_head_sha.return_value = "abc123"
    result3 = asyncio.run(cache.get_safe_sha(deps))
    assert result3 is result1


# ── get_safe_sha: clean tree wait ────────────────────────────────────────────


def test_get_safe_sha_waits_for_clean_working_tree(tmp_path, git_svc, github_svc):
    git_svc.is_working_tree_clean.side_effect = [False, True]
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)


# ── get_safe_sha: parallel callers serialise ─────────────────────────────────


def test_get_safe_sha_parallel_callers_run_preflight_once(
    tmp_path, git_svc, github_svc
):
    """Concurrent callers at the same SHA must serialise on the lock and observe
    a single preflight run — only one run_preflight call total."""
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    async def _run_two():
        results = await asyncio.gather(
            cache.get_safe_sha(deps),
            cache.get_safe_sha(deps),
        )
        return results

    results = asyncio.run(_run_two())

    assert all(isinstance(r, PreflightReady) for r in results)
    assert results[0] is results[1]
    assert len(fake.preflight_calls) == 1


# ── handle_preflight_failure: AFK slice-mode label validation ─────────────────


def test_get_safe_sha_raises_when_afk_issue_missing_slice_mode_label(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=42, labels=["bug", "ready-for-agent"])],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(RuntimeError, match="Pre-Flight Reporter"):
        asyncio.run(cache.get_safe_sha(deps))


def test_get_safe_sha_does_not_validate_slice_label_on_hitl_branch(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [IssueOutput(number=7, labels=["bug", "ready-for-human"])],
        preflight_responses=[[("mypy", "mypy .", "error")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 7


def test_get_safe_sha_raises_when_afk_issue_has_multiple_slice_mode_labels(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner(
        [
            IssueOutput(
                number=13,
                labels=["ready-for-agent", "behavior-slice", "refactor-slice"],
            )
        ],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(RuntimeError, match="Pre-Flight Reporter"):
        asyncio.run(cache.get_safe_sha(deps))


# ── get_safe_sha: divergence resolution via agent ────────────────────────────


def _setup_worktree_mocks(git_svc):
    """Configure git_svc to support managed_worktree (creates real dirs)."""
    _registered: list = []

    def _fake_create_worktree(repo, path, branch, sha=None):
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text("[project]\n")
        _registered.append(path)

    def _fake_remove_worktree(repo, path):
        shutil.rmtree(path, ignore_errors=True)
        _registered[:] = [p for p in _registered if p != path]

    git_svc.verify_ref_exists.return_value = False
    git_svc.list_worktrees.side_effect = lambda repo: list(_registered)
    git_svc.create_worktree.side_effect = _fake_create_worktree
    git_svc.remove_worktree.side_effect = _fake_remove_worktree


def test_get_safe_sha_resolves_divergence_via_agent_and_returns_ready(
    tmp_path, git_svc, github_svc
):
    """When pull_with_merge_fallback raises a textual-conflict error, get_safe_sha
    spawns the divergence-resolution agent; on success it fast-forwards main and
    returns PreflightReady with the post-merge SHA."""
    _setup_worktree_mocks(git_svc)

    git_svc.pull_with_merge_fallback.side_effect = GitCommandError(
        "git merge origin/main failed due to conflicts"
    )
    git_svc.get_current_branch.return_value = "main"
    git_svc.get_head_sha.side_effect = ["abc123", "merged-sha"]

    fake = FakeAgentRunner([CompletionOutput()], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "merged-sha"
    git_svc.fast_forward_branch.assert_called_once()


def test_get_safe_sha_propagates_pull_error_when_divergence_agent_fails(
    tmp_path, git_svc, github_svc
):
    """When the divergence agent fails (FailedOutput), the original GitCommandError
    propagates and no PreflightReady is returned."""
    from pycastle.agents.output_protocol import FailedOutput

    _setup_worktree_mocks(git_svc)

    pull_err = GitCommandError("git merge origin/main failed due to conflicts")
    git_svc.pull_with_merge_fallback.side_effect = pull_err
    git_svc.get_current_branch.return_value = "main"
    git_svc.get_head_sha.return_value = "abc123"

    fake = FakeAgentRunner([FailedOutput()], preflight_responses=[])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(GitCommandError) as exc_info:
        asyncio.run(cache.get_safe_sha(deps))

    assert exc_info.value is pull_err


def test_get_safe_sha_propagates_non_conflict_pull_error_without_spawning_agent(
    tmp_path, git_svc, github_svc
):
    """Auth/unreachable pull errors are propagated immediately without spawning
    the divergence-resolution agent."""
    git_svc.pull_with_merge_fallback.side_effect = GitCommandError(
        "git pull --ff-only failed", stderr="authentication failed"
    )

    fake = FakeAgentRunner([], preflight_responses=[])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(GitCommandError):
        asyncio.run(cache.get_safe_sha(deps))

    assert len(fake.calls) == 0


# ── get_safe_sha: unrelated histories auto-recovery ──────────────────────────


def test_get_safe_sha_auto_recovers_when_unrelated_histories_and_no_local_commits(
    tmp_path, git_svc, github_svc
):
    """When pull fails with unrelated histories and local has 0 commits ahead of
    origin, get_safe_sha hard-resets to origin/<branch> and returns PreflightReady."""
    from pycastle.services.git_service import UnrelatedHistoriesError

    git_svc.pull_with_merge_fallback.side_effect = UnrelatedHistoriesError(
        "git merge --no-edit 'origin/main' failed",
        returncode=128,
        stderr="fatal: refusing to merge unrelated histories",
    )
    git_svc.get_current_branch.return_value = "main"
    git_svc.count_commits_ahead.return_value = 0

    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    result = asyncio.run(cache.get_safe_sha(deps))

    assert isinstance(result, PreflightReady)
    git_svc.hard_reset_to.assert_called_once_with(tmp_path, "origin/main")


def test_get_safe_sha_halts_with_guidance_when_unrelated_histories_and_local_commits(
    tmp_path, git_svc, github_svc
):
    """When pull fails with unrelated histories and local has commits not on origin,
    get_safe_sha raises and the error message contains the recovery command."""
    from pycastle.services.git_service import UnrelatedHistoriesError

    git_svc.pull_with_merge_fallback.side_effect = UnrelatedHistoriesError(
        "git merge --no-edit 'origin/main' failed",
        returncode=128,
        stderr="fatal: refusing to merge unrelated histories",
    )
    git_svc.get_current_branch.return_value = "main"
    git_svc.count_commits_ahead.return_value = 2
    git_svc.get_local_only_commit_subjects.return_value = [
        "fix: something",
        "feat: another thing",
    ]

    display = PlainStatusDisplay()
    printed: list[tuple[str, str]] = []
    original_print = display.print

    def capturing_print(caller, message, style=None):
        printed.append((style or "", message))
        original_print(caller, message, style=style)

    display.print = capturing_print  # type: ignore[method-assign]

    fake = FakeAgentRunner([], preflight_responses=[])

    @dataclasses.dataclass
    class _DepsWith:
        git_svc: GitService
        github_svc: GithubService
        cfg: Config
        status_display: PlainStatusDisplay
        agent_runner: FakeAgentRunner
        repo_root: Path

    deps = _DepsWith(
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=fake,
        cfg=Config(max_parallel=4, max_iterations=1),
        status_display=display,
    )
    cache = PreflightCache()

    with pytest.raises((GitCommandError, Exception)):
        asyncio.run(cache.get_safe_sha(deps))

    git_svc.hard_reset_to.assert_not_called()
    assert any(
        "git fetch origin && git reset --hard origin/main" in msg for _, msg in printed
    )


def test_get_safe_sha_does_not_spawn_divergence_resolver_on_unrelated_histories(
    tmp_path, git_svc, github_svc
):
    """Unrelated-histories failure must never route to the divergence-resolver agent."""
    from pycastle.services.git_service import UnrelatedHistoriesError

    git_svc.pull_with_merge_fallback.side_effect = UnrelatedHistoriesError(
        "git merge --no-edit 'origin/main' failed",
        returncode=128,
        stderr="fatal: refusing to merge unrelated histories",
    )
    git_svc.get_current_branch.return_value = "main"
    git_svc.count_commits_ahead.return_value = 3
    git_svc.get_local_only_commit_subjects.return_value = ["fix: something"]

    fake = FakeAgentRunner([], preflight_responses=[])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    cache = PreflightCache()

    with pytest.raises(Exception):
        asyncio.run(cache.get_safe_sha(deps))

    assert len(fake.calls) == 0
