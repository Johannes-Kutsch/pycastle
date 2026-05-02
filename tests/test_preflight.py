import asyncio
import dataclasses

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pycastle.config import Config
from pycastle.services import GitCommandError, GitService
from pycastle.services import GithubService
from pycastle.iteration._deps import (
    Deps,
    FakeAgentRunner,
    RecordingLogger,
    RecordingStatusDisplay,
)
from pycastle.status_display import PlainStatusDisplay
from pycastle.iteration.preflight import (
    PreflightAFK,
    PreflightHITL,
    PreflightReady,
    preflight_phase,
)


@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    return svc


@pytest.fixture
def github_svc():
    svc = MagicMock(spec=GithubService)
    svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    return svc


@pytest.fixture
def logger():
    return RecordingLogger()


def _make_deps(tmp_path, agent_runner, *, git_svc, github_svc, logger):
    return Deps(
        env={},
        repo_root=tmp_path,
        git_svc=git_svc,
        github_svc=github_svc,
        agent_runner=agent_runner,
        cfg=Config(max_parallel=4, max_iterations=1),
        logger=logger,
        status_display=PlainStatusDisplay(),
    )


# ── preflight_phase: no open issues ──────────────────────────────────────────


def test_preflight_phase_returns_ready_with_empty_issues_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"
    assert result.issues == []


def test_preflight_phase_does_not_call_run_preflight_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    assert fake.preflight_calls == [], (
        f"run_preflight must not be called when no issues; got {fake.preflight_calls}"
    )


def test_preflight_phase_does_not_create_worktree_when_no_open_issues(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    git_svc.checkout_detached.assert_not_called()


# ── preflight_phase: preflight passes ────────────────────────────────────────


def test_preflight_phase_returns_ready_with_open_issues_when_preflight_passes(
    tmp_path, git_svc, github_svc, logger
):
    issues = [{"number": 1, "title": "Fix bug", "body": ""}]
    github_svc.get_open_issues.return_value = issues
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "abc123"
    assert result.issues == issues


def test_preflight_phase_calls_checkout_detached_with_head_sha(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-flight-sandbox"
    git_svc.checkout_detached.assert_called_once_with(
        tmp_path, expected_worktree, "abc123"
    )


def test_preflight_phase_passes_worktree_path_as_mount_path_to_run_preflight(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-flight-sandbox"
    assert len(fake.preflight_calls) == 1
    assert fake.preflight_calls[0]["mount_path"] == expected_worktree


# ── preflight_phase: HITL routing ────────────────────────────────────────────


def test_preflight_phase_returns_hitl_on_hitl_preflight_verdict(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ['<issue>{"number": 55, "labels": ["bug", "ready-for-human"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 55
    assert result.worktree_sha == "abc123"


# ── preflight_phase: AFK routing ─────────────────────────────────────────────


def test_preflight_phase_returns_afk_on_afk_preflight_verdict(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Fix preflight issue"
    fake = FakeAgentRunner(
        ['<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightAFK)
    assert result.issues == [{"number": 42, "title": "Fix preflight issue"}]
    assert result.worktree_sha == "abc123"


def test_preflight_phase_hitl_routing_uses_configured_hitl_label(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ['<issue>{"number": 55, "labels": ["custom-bug", "custom-human"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(
        deps,
        cfg=Config(
            max_parallel=4,
            max_iterations=1,
            bug_label="custom-bug",
            issue_label="custom-agent",
            hitl_label="custom-human",
        ),
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightHITL)
    assert result.issue_number == 55


# ── preflight_phase: IssueParseError → RuntimeError ─────────────────────────


def test_preflight_phase_raises_runtime_error_when_preflight_agent_returns_no_issue_tag(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ["<promise>COMPLETE</promise>no issue tag here"],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(RuntimeError, match="issue"):
        asyncio.run(preflight_phase(deps))


# ── preflight_phase: worktree lifecycle ──────────────────────────────────────


def test_preflight_phase_removes_worktree_after_passing_preflight(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-flight-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_preflight_phase_removes_worktree_when_preflight_fails(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Preflight issue"
    fake = FakeAgentRunner(
        ['<issue>{"number": 42, "labels": ["bug", "ready-for-agent"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-flight-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


def test_preflight_phase_removes_worktree_when_exception_raised(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[RuntimeError("container error")])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    with pytest.raises(RuntimeError, match="container error"):
        asyncio.run(preflight_phase(deps))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "pre-flight-sandbox"
    git_svc.remove_worktree.assert_called_once_with(tmp_path, expected_worktree)


# ── preflight_phase: status_display threading ────────────────────────────────


def test_preflight_phase_passes_status_display_to_run_preflight(
    tmp_path, git_svc, github_svc, logger
):
    recording = RecordingStatusDisplay()
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(deps, status_display=recording)

    asyncio.run(preflight_phase(deps))

    assert len(fake.preflight_calls) == 1
    assert fake.preflight_calls[0]["status_display"] is recording


def test_preflight_phase_uses_pre_flight_as_run_preflight_name(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    asyncio.run(preflight_phase(deps))

    assert len(fake.preflight_calls) == 1
    assert fake.preflight_calls[0]["name"] == "Pre-Flight"


def test_preflight_phase_passes_checking_work_body_to_run_preflight(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    asyncio.run(preflight_phase(deps))

    assert fake.preflight_calls[0]["work_body"] == "Checking"


def test_preflight_phase_passes_preflight_stage_string_to_run_preflight(
    tmp_path, git_svc, github_svc, logger
):
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    asyncio.run(preflight_phase(deps))

    assert len(fake.preflight_calls) == 1
    assert fake.preflight_calls[0]["stage"] == "PREFLIGHT"


def test_preflight_phase_returns_ready_when_all_checks_pass(
    tmp_path, git_svc, github_svc, logger
):
    github_svc.get_open_issues.return_value = [
        {"number": 1, "title": "Fix bug", "body": ""}
    ]
    fake = FakeAgentRunner([], preflight_responses=[[]])
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightReady)
    assert result.issues


def test_preflight_phase_prints_no_confirmation_when_no_open_issues(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])
    recording = RecordingStatusDisplay()

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(deps, status_display=recording)

    asyncio.run(preflight_phase(deps))

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert "Preflight checks passed." not in print_messages


def test_preflight_phase_prints_no_confirmation_when_check_fails(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    github_svc.get_issue_title.return_value = "Fix bug"
    fake = FakeAgentRunner(
        ['<issue>{"number": 42, "labels": ["ready-for-agent"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    recording = RecordingStatusDisplay()

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(deps, status_display=recording)

    asyncio.run(preflight_phase(deps))

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert "Preflight checks passed." not in print_messages


def test_preflight_failure_uses_pre_flight_reporter_as_agent_name(
    tmp_path, git_svc, logger
):
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ['<issue>{"number": 42, "labels": ["ready-for-human"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    asyncio.run(preflight_phase(deps))

    assert len(fake.calls) == 1
    assert fake.calls[0].name == "Pre-Flight Reporter"


def test_preflight_failure_passes_reporting_work_body_to_run(tmp_path, git_svc, logger):
    check_name = "ruff"
    github_svc = MagicMock(spec=GithubService)
    github_svc.get_open_issues.return_value = [{"number": 1, "title": "Fix bug"}]
    fake = FakeAgentRunner(
        ['<issue>{"number": 42, "labels": ["ready-for-human"]}</issue>'],
        preflight_responses=[[("ruff", "ruff check .", "E501")]],
    )
    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    asyncio.run(preflight_phase(deps))

    assert fake.calls[0].work_body == f"reporting {check_name} issue"


# ── preflight_phase: preflight pull ──────────────────────────────────────────


def test_preflight_phase_propagates_error_when_pull_fails(
    tmp_path, git_svc, github_svc, logger
):
    git_svc.pull.side_effect = GitCommandError("git pull --ff-only failed")
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )

    with pytest.raises(GitCommandError):
        asyncio.run(preflight_phase(deps))

    git_svc.get_head_sha.assert_not_called()


def test_preflight_phase_prints_red_error_message_when_pull_fails(
    tmp_path, git_svc, github_svc, logger
):
    git_svc.pull.side_effect = GitCommandError("git pull --ff-only failed")
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])
    recording = RecordingStatusDisplay()

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(deps, status_display=recording)

    with pytest.raises(GitCommandError):
        asyncio.run(preflight_phase(deps))

    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    expected = (
        "[red]git pull --ff-only failed — remote branch has diverged or is unreachable. "
        "Resolve manually and retry.[/red]"
    )
    assert expected in print_messages


def test_preflight_phase_pins_sha_from_post_pull_head(tmp_path, logger):
    git_svc = MagicMock(spec=GitService)
    git_svc.is_working_tree_clean.return_value = True
    git_svc.get_head_sha.return_value = "post_pull_sha"
    github_svc = MagicMock(spec=GithubService)
    issues = [{"number": 1, "title": "Fix bug", "body": ""}]
    github_svc.get_open_issues.return_value = issues
    fake = FakeAgentRunner([], preflight_responses=[[]])

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightReady)
    assert result.sha == "post_pull_sha"
    git_svc.pull.assert_called_once_with(tmp_path)


def test_preflight_phase_waits_for_clean_working_tree_before_pulling(
    tmp_path, git_svc, github_svc, logger
):
    git_svc.is_working_tree_clean.side_effect = [False, True]
    github_svc.get_open_issues.return_value = []
    fake = FakeAgentRunner([], preflight_responses=[])
    recording = RecordingStatusDisplay()

    deps = _make_deps(
        tmp_path, fake, git_svc=git_svc, github_svc=github_svc, logger=logger
    )
    deps = dataclasses.replace(deps, status_display=recording)

    with patch("pycastle.iteration._utils.asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(preflight_phase(deps))

    assert isinstance(result, PreflightReady)
    print_messages = [c[2] for c in recording.calls if c[0] == "print"]
    assert any("preflight" in msg for msg in print_messages)
