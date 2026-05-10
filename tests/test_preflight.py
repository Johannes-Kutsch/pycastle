import asyncio
import dataclasses
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch

from pycastle.agent_output_protocol import (
    IssueOutput,
)
from pycastle.config import Config
from pycastle.services import GitCommandError, GitService
from pycastle.services import GithubService
from pycastle.iteration._deps import (
    FakeAgentRunner,
)
from pycastle.status_display import PlainStatusDisplay
from pycastle.iteration.preflight import (
    PreflightAFK,
    PreflightHITL,
    PreflightReady,
    ensure_preflight,
)


@dataclasses.dataclass
class _PreflightStub:
    git_svc: GitService
    github_svc: GithubService
    cfg: Config
    status_display: PlainStatusDisplay
    agent_runner: FakeAgentRunner
    repo_root: Path
    preflight_verdict: PreflightReady | None = None


@pytest.fixture
def git_svc():
    from unittest.mock import MagicMock

    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    return svc


@pytest.fixture
def github_svc():
    from unittest.mock import MagicMock

    svc = MagicMock(spec=GithubService)
    return svc


def _make_deps(tmp_path, agent_runner, *, git_svc, github_svc):
    return _PreflightStub(
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=agent_runner,
        cfg=Config(max_parallel=4, max_iterations=1),
        status_display=PlainStatusDisplay(),
    )


# ── ensure_preflight: basic return variants ───────────────────────────────────


def test_ensure_preflight_returns_ready_with_sha_when_checks_pass(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    result = asyncio.run(ensure_preflight(deps, tmp_path))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"


def test_ensure_preflight_returns_hitl_when_checks_fail_with_hitl_label(
    tmp_path, git_svc
):
    from unittest.mock import MagicMock

    github_svc = MagicMock(spec=GithubService)
    fake = FakeAgentRunner(
        [IssueOutput(number=55, labels=["bug", "ready-for-human"])],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    result = asyncio.run(ensure_preflight(deps, tmp_path))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 55
    assert result.worktree_sha == "abc123"


def test_ensure_preflight_returns_afk_when_checks_fail_with_afk_label(
    tmp_path, git_svc
):
    from unittest.mock import MagicMock

    github_svc = MagicMock(spec=GithubService)
    fake = FakeAgentRunner(
        [IssueOutput(number=42, labels=["bug", "ready-for-agent"])],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    result = asyncio.run(ensure_preflight(deps, tmp_path))

    assert isinstance(result, PreflightAFK)
    assert result.issue_number == 42
    assert result.sha == "abc123"


# ── ensure_preflight: memoization ────────────────────────────────────────────


def test_ensure_preflight_calls_run_preflight_once_across_two_invocations(
    tmp_path, git_svc, github_svc
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    result1 = asyncio.run(ensure_preflight(deps, tmp_path))
    result2 = asyncio.run(ensure_preflight(deps, tmp_path))

    assert isinstance(result1, PreflightReady)
    assert result2 == result1
    assert len(fake.preflight_calls) == 1


# ── ensure_preflight: pull failure ───────────────────────────────────────────


def test_ensure_preflight_propagates_git_command_error_on_pull_failure(
    tmp_path, git_svc, github_svc
):
    git_svc.pull.side_effect = GitCommandError("git pull --ff-only failed")
    fake = FakeAgentRunner([], preflight_responses=[])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    with pytest.raises(GitCommandError):
        asyncio.run(ensure_preflight(deps, tmp_path))

    git_svc.get_head_sha.assert_not_called()


def test_ensure_preflight_waits_for_clean_working_tree(tmp_path, git_svc, github_svc):
    git_svc.is_working_tree_clean.side_effect = [False, True]
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)

    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(ensure_preflight(deps, tmp_path))

    assert isinstance(result, PreflightReady)
