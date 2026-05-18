import asyncio

import pytest
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import (
    CompletionOutput,
    PlanParseError,
    PlannerOutput,
)
from pycastle.services import GitService
from pycastle.iteration._deps import FakeAgentRunner, RecordingStatusDisplay, _make_deps
from pycastle.iteration.planning import (
    AllBlocked,
    PlanReady,
    hydrate_planned_issues,
    partition_by_slice_label,
    planning_phase,
)


def _plan_output(issues: list[dict]) -> PlannerOutput:
    return PlannerOutput(
        issues=[{"number": i["number"], "title": i["title"]} for i in issues]
    )


@pytest.fixture
def git_svc():
    svc = MagicMock(spec=GitService)
    svc.get_head_sha.return_value = "abc123"
    svc.is_working_tree_clean.return_value = True
    return svc


# ── planning_phase: skip paths ───────────────────────────────────────────────


def test_planning_phase_skips_planner_when_in_flight(tmp_path, git_svc):
    issues = [{"number": 1, "title": "A", "body": "", "comments": []}]
    fake = FakeAgentRunner([])  # no agent calls expected

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, [], in_flight=issues))

    assert isinstance(result, PlanReady)
    assert result.issues == issues
    assert len(fake.calls) == 0, "No agent must be called for in-flight skip"
    git_svc.create_worktree.assert_not_called()


def test_planning_phase_skip_in_flight_emits_plan_row(tmp_path, git_svc):
    issues = [{"number": 7, "title": "B", "body": "", "comments": []}]
    recording = RecordingStatusDisplay()
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, status_display=recording)
    asyncio.run(planning_phase(deps, issues, [], in_flight=issues))

    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "[Plan] row must be removed on in-flight skip"
    msg = plan_removes[0][2]
    assert "in-flight" in msg
    assert "#7" in msg
    assert "skipping plan agent" in msg


def test_planning_phase_skips_planner_for_single_issue(tmp_path, git_svc):
    issues = [
        {
            "number": 5,
            "title": "Solo",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    fake = FakeAgentRunner([])  # no agent calls expected

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, PlanReady)
    assert result.issues == issues
    assert len(fake.calls) == 0, "No agent must be called for single-issue skip"
    git_svc.create_worktree.assert_not_called()


def test_planning_phase_skip_single_issue_emits_plan_row(tmp_path, git_svc):
    issues = [
        {
            "number": 42,
            "title": "Solo",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    recording = RecordingStatusDisplay()
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, status_display=recording)
    asyncio.run(planning_phase(deps, issues, []))

    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "[Plan] row must be removed on single-issue skip"
    msg = plan_removes[0][2]
    assert "#42" in msg
    assert "skipping plan agent" in msg


# ── planning_phase: returns PlanReady with sorted issues ────────────────────


def test_planning_phase_returns_plan_ready_with_issues_sorted_by_number(
    tmp_path, git_svc
):
    issues = [
        {
            "number": 3,
            "title": "C",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([_plan_output(issues)])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, PlanReady)
    assert [i["number"] for i in result.issues] == [1, 2, 3]


def test_planning_phase_passes_ready_for_agent_issues_as_json_to_planner(
    tmp_path, git_svc
):
    import json

    issues = [
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([_plan_output(issues)])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    asyncio.run(planning_phase(deps, issues, []))

    assert fake.calls[0].scope_args["READY_FOR_AGENT_ISSUES_JSON"] == json.dumps(issues)


def test_planning_phase_passes_all_open_issues_as_json_to_planner(tmp_path, git_svc):
    import json

    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    all_open = [
        {"number": 1, "title": "A", "labels": ["ready-for-agent"]},
        {"number": 2, "title": "B", "labels": ["ready-for-human"]},
    ]
    fake = FakeAgentRunner([_plan_output(issues)])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    asyncio.run(planning_phase(deps, issues, all_open))

    assert fake.calls[0].scope_args["ALL_OPEN_ISSUES_JSON"] == json.dumps(all_open)


# ── planning_phase: worktree lifecycle ──────────────────────────────────────


def test_planning_phase_removes_worktree_after_success(tmp_path, git_svc):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([_plan_output(issues)])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    asyncio.run(planning_phase(deps, issues, []))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_any_call(tmp_path, expected_worktree)


def test_planning_phase_removes_worktree_when_exception_raised(tmp_path, git_svc):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([RuntimeError("agent crashed")])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    with pytest.raises(RuntimeError, match="agent crashed"):
        asyncio.run(planning_phase(deps, issues, []))

    expected_worktree = tmp_path / "pycastle" / ".worktrees" / "plan-sandbox"
    git_svc.remove_worktree.assert_any_call(tmp_path, expected_worktree)


# ── planning_phase: error paths ─────────────────────────────────────────────


def test_planning_phase_raises_runtime_error_when_planner_output_has_no_plan_tag(
    tmp_path, git_svc
):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([PlanParseError("Planner produced no <plan> tag.")])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    with pytest.raises(RuntimeError, match="no <plan> tag"):
        asyncio.run(planning_phase(deps, issues, []))


def test_planning_phase_raises_runtime_error_when_planner_returns_wrong_output_type(
    tmp_path, git_svc
):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([CompletionOutput()])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    with pytest.raises(RuntimeError, match="unexpected output type"):
        asyncio.run(planning_phase(deps, issues, []))


# ── planning_phase: edge cases ───────────────────────────────────────────────


def test_planning_phase_returns_all_blocked_when_planner_emits_empty_issues_list(
    tmp_path, git_svc
):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([_plan_output([])])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == []
    assert len(fake.calls) == 1


def test_planning_phase_all_blocked_carries_blocked_list(tmp_path, git_svc):
    blocked = [{"number": 5, "blocked_by": 3, "reason": "depends on #3"}]
    output = PlannerOutput(issues=[], blocked=blocked)
    issues = [
        {
            "number": 5,
            "title": "X",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 3,
            "title": "Y",
            "body": "",
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([output])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == blocked


# ── hydrate_planned_issues ──────────────────────────────────────────────────


def test_hydrate_planned_issues_merges_body_and_comments_from_open_issues():
    plan = PlanReady(
        issues=[{"number": 1, "title": "A"}, {"number": 2, "title": "B"}],
        sha="abc123",
    )
    open_issues = [
        {
            "number": 1,
            "title": "A",
            "body": "body of A",
            "comments": [{"author": "x", "created_at": "t", "body": "hi"}],
            "labels": ["ready-for-agent", "docs-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "body of B",
            "comments": [],
            "labels": ["ready-for-agent", "refactor-slice"],
        },
    ]

    result = hydrate_planned_issues(plan, open_issues)

    assert result.issues[0]["number"] == 1
    assert result.issues[0]["body"] == "body of A"
    assert result.issues[0]["comments"] == [
        {"author": "x", "created_at": "t", "body": "hi"}
    ]
    assert result.issues[0]["labels"] == ["ready-for-agent", "docs-slice"]
    assert result.issues[1]["number"] == 2
    assert result.issues[1]["body"] == "body of B"
    assert result.issues[1]["comments"] == []
    assert result.issues[1]["labels"] == ["ready-for-agent", "refactor-slice"]


def test_hydrate_planned_issues_raises_when_planned_number_not_in_open_issues():
    plan = PlanReady(
        issues=[{"number": 99, "title": "Hallucinated"}],
        sha="abc123",
    )
    open_issues = [
        {"number": 1, "title": "A", "body": "x", "comments": [], "labels": []},
    ]

    with pytest.raises(RuntimeError, match="#99"):
        hydrate_planned_issues(plan, open_issues)


# ── Config.needs_slice_type_label ────────────────────────────────────────────


def test_config_needs_slice_type_label_defaults_to_needs_slice_type():
    from pycastle.config import Config

    assert Config().needs_slice_type_label == "needs-slice-type"


# ── partition_by_slice_label ─────────────────────────────────────────────────


def test_partition_by_slice_label_separates_well_formed_and_malformed():
    from pycastle.config import Config

    cfg = Config()
    well = {"number": 1, "labels": ["ready-for-agent", "behavior-slice"]}
    no_slice = {"number": 2, "labels": ["ready-for-agent"]}
    multi_slice = {
        "number": 3,
        "labels": ["ready-for-agent", "behavior-slice", "refactor-slice"],
    }

    good, bad = partition_by_slice_label([well, no_slice, multi_slice], cfg)

    assert good == [well]
    assert bad == [no_slice, multi_slice]


def test_planning_phase_all_open_issues_json_unaffected_by_partition(tmp_path, git_svc):
    import json

    well1 = {
        "number": 1,
        "title": "A",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    well2 = {
        "number": 2,
        "title": "B",
        "body": "",
        "comments": [],
        "labels": ["refactor-slice"],
    }
    malformed = {"number": 3, "title": "C", "body": "", "comments": [], "labels": []}
    all_open = [
        {"number": 1, "title": "A", "labels": ["ready-for-agent", "behavior-slice"]},
        {"number": 2, "title": "B", "labels": ["ready-for-agent", "refactor-slice"]},
        {"number": 3, "title": "C", "labels": ["ready-for-agent"]},
        {"number": 4, "title": "D", "labels": ["ready-for-human"]},
    ]
    fake = FakeAgentRunner([_plan_output([well1, well2])])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    asyncio.run(planning_phase(deps, [well1, well2, malformed], all_open))

    assert fake.calls[0].scope_args["ALL_OPEN_ISSUES_JSON"] == json.dumps(all_open)


def test_planning_phase_adds_label_and_comment_for_malformed_without_flag(
    tmp_path, git_svc
):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well = {
        "number": 1,
        "title": "A",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    malformed = {"number": 2, "title": "B", "body": "", "comments": [], "labels": []}
    fake = FakeAgentRunner([_plan_output([well])])
    github_svc = MagicMock(spec=GithubService)

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    asyncio.run(planning_phase(deps, [well, malformed], []))

    github_svc.add_label_to_issue.assert_called_once_with(2, "needs-slice-type")
    github_svc.post_comment.assert_called_once()
    comment_body = github_svc.post_comment.call_args[0][1]
    assert "ready-for-agent" in comment_body
    assert "none" in comment_body


def test_planning_phase_makes_no_calls_for_malformed_already_flagged(tmp_path, git_svc):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well = {
        "number": 1,
        "title": "A",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    malformed = {
        "number": 2,
        "title": "B",
        "body": "",
        "comments": [],
        "labels": ["needs-slice-type"],
    }
    fake = FakeAgentRunner([_plan_output([well])])
    github_svc = MagicMock(spec=GithubService)

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    asyncio.run(planning_phase(deps, [well, malformed], []))

    github_svc.add_label_to_issue.assert_not_called()
    github_svc.post_comment.assert_not_called()
    github_svc.remove_label_from_issue.assert_not_called()


def test_planning_phase_removes_stale_flag_from_well_formed_issue(tmp_path, git_svc):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well1 = {
        "number": 1,
        "title": "A",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice", "needs-slice-type"],
    }
    well2 = {
        "number": 2,
        "title": "B",
        "body": "",
        "comments": [],
        "labels": ["refactor-slice"],
    }
    fake = FakeAgentRunner([_plan_output([well1, well2])])
    github_svc = MagicMock(spec=GithubService)

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    asyncio.run(planning_phase(deps, [well1, well2], []))

    github_svc.remove_label_from_issue.assert_called_once_with(1, "needs-slice-type")
    github_svc.add_label_to_issue.assert_not_called()
    github_svc.post_comment.assert_not_called()


def test_planning_phase_makes_no_calls_for_well_formed_without_flag(tmp_path, git_svc):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well1 = {
        "number": 1,
        "title": "A",
        "body": "",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    well2 = {
        "number": 2,
        "title": "B",
        "body": "",
        "comments": [],
        "labels": ["refactor-slice"],
    }
    fake = FakeAgentRunner([_plan_output([well1, well2])])
    github_svc = MagicMock(spec=GithubService)

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    asyncio.run(planning_phase(deps, [well1, well2], []))

    github_svc.add_label_to_issue.assert_not_called()
    github_svc.post_comment.assert_not_called()
    github_svc.remove_label_from_issue.assert_not_called()


def test_planning_phase_filters_malformed_from_ready_for_agent_json(tmp_path, git_svc):
    import json

    well1 = {
        "number": 1,
        "title": "A",
        "body": "",
        "comments": [],
        "labels": ["ready-for-agent", "behavior-slice"],
    }
    well2 = {
        "number": 2,
        "title": "B",
        "body": "",
        "comments": [],
        "labels": ["ready-for-agent", "refactor-slice"],
    }
    malformed = {
        "number": 3,
        "title": "C",
        "body": "",
        "comments": [],
        "labels": ["ready-for-agent"],
    }
    fake = FakeAgentRunner([_plan_output([well1, well2])])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    asyncio.run(planning_phase(deps, [well1, well2, malformed], []))

    assert fake.calls[0].scope_args["READY_FOR_AGENT_ISSUES_JSON"] == json.dumps(
        [well1, well2]
    )
