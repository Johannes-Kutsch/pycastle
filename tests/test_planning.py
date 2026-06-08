import asyncio

import pytest
from unittest.mock import MagicMock

from pycastle.agents.output_protocol import (
    AgentRole,
    CompletionOutput,
    PlanParseError,
    PlannerOutput,
)
from pycastle.config import Config, StageOverride
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.services import GitService
from tests.support import (
    FakeAgentRunner,
    RecordingStatusDisplay,
    StubPreflightCache,
    _make_deps,
)
from pycastle.iteration.planning_issue_intake import (
    PlanReady,
    hydrate_planned_issues,
    prepare_planning_issue_set,
)
from pycastle.iteration.planning import (
    AllBlocked,
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
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        }
    ]
    fake = FakeAgentRunner([])  # no agent calls expected

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, PlanReady)
    assert len(result.issues) == 1
    assert result.issues[0] == {
        **issues[0],
        "readiness": result.readiness_by_number[5],
    }
    assert len(fake.calls) == 0, "No agent must be called for single-issue skip"
    git_svc.create_worktree.assert_not_called()


def test_planning_phase_single_issue_skip_uses_prepared_issue_body(tmp_path, git_svc):
    issue = {
        "number": 5,
        "title": "Solo",
        "body": "Summary\n\nBlocked by #99\n\n" + ("x" * 120),
        "comments": None,
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, [issue], []))

    assert isinstance(result, PlanReady)
    assert result.issues == [
        {
            "number": 5,
            "title": "Solo",
            "body": "Summary\n\n" + ("x" * 120),
            "comments": [],
            "labels": ["behavior-slice"],
            "readiness": result.readiness_by_number[5],
        }
    ]


def test_planning_phase_single_issue_skip_uses_supplied_planning_issue_intake_result(
    tmp_path, git_svc
):
    issue = {
        "number": 5,
        "title": "Solo",
        "body": "Summary\n\nBlocked by #99\n\n" + ("x" * 120),
        "comments": None,
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc)

    result = asyncio.run(
        planning_phase(
            deps,
            [issue],
            [],
            prepared_issue_set=prepare_planning_issue_set([issue], deps.cfg),
        )
    )

    assert isinstance(result, PlanReady)
    assert result.issues == [
        {
            "number": 5,
            "title": "Solo",
            "body": "Summary\n\n" + ("x" * 120),
            "comments": [],
            "labels": ["behavior-slice"],
            "readiness": result.readiness_by_number[5],
        }
    ]


def test_planning_phase_skip_single_issue_emits_plan_row(tmp_path, git_svc):
    issues = [
        {
            "number": 42,
            "title": "Solo",
            "body": "x" * 100,
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


# ── planning_phase: readiness_by_number propagation ─────────────────────────


def test_planning_phase_single_issue_skip_populates_readiness_by_number(
    tmp_path, git_svc
):
    from pycastle.issue_readiness import IssueReadiness, SliceMode

    issue = {
        "number": 5,
        "title": "Solo",
        "body": "x" * 100,
        "comments": [],
        "labels": ["docs-slice"],
    }
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, [issue], []))

    assert isinstance(result, PlanReady)
    assert 5 in result.readiness_by_number
    readiness = result.readiness_by_number[5]
    assert isinstance(readiness, IssueReadiness)
    assert readiness.selected_mode == SliceMode.DOCS


def test_planning_phase_multi_issue_planner_populates_readiness_by_number(
    tmp_path, git_svc
):
    from pycastle.issue_readiness import SliceMode

    issue_a = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    issue_b = {
        "number": 2,
        "title": "B",
        "body": "x" * 100,
        "comments": [],
        "labels": ["refactor-slice"],
    }
    fake = FakeAgentRunner([_plan_output([issue_a, issue_b])])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, [issue_a, issue_b], []))

    assert isinstance(result, PlanReady)
    assert result.readiness_by_number[1].selected_mode == SliceMode.BEHAVIOR
    assert result.readiness_by_number[2].selected_mode == SliceMode.REFACTOR


def test_planning_phase_malformed_issue_absent_from_readiness_by_number(
    tmp_path, git_svc
):
    well = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    malformed = {
        "number": 2,
        "title": "B",
        "body": "x" * 100,
        "comments": [],
        "labels": [],
    }
    fake = FakeAgentRunner([_plan_output([well])])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, [well, malformed], []))

    assert isinstance(result, PlanReady)
    assert 1 in result.readiness_by_number
    assert 2 not in result.readiness_by_number


# ── planning_phase: returns PlanReady with sorted issues ────────────────────


def test_planning_phase_returns_plan_ready_with_issues_sorted_by_number(
    tmp_path, git_svc
):
    issues = [
        {
            "number": 3,
            "title": "C",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 1,
            "title": "A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([_plan_output(issues)])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, PlanReady)
    assert [i["number"] for i in result.issues] == [1, 2, 3]


def test_planning_phase_uses_prepared_issue_fields_when_resolving_planner_output(
    tmp_path, git_svc
):
    from pycastle.issue_readiness import SliceMode

    source_issue = {
        "number": 1,
        "title": "Source title",
        "body": "Summary\n\nBlocked by #99\n\n" + ("x" * 120),
        "comments": None,
        "labels": ["behavior-slice"],
    }
    planner_issue = {"number": 1, "title": "Planner title"}
    fake = FakeAgentRunner([PlannerOutput(issues=[planner_issue])])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(
        planning_phase(
            deps,
            [
                source_issue,
                {
                    "number": 2,
                    "title": "Other",
                    "body": "x" * 120,
                    "comments": [],
                    "labels": ["behavior-slice"],
                },
            ],
            [],
        )
    )

    assert isinstance(result, PlanReady)
    assert result.issues == [
        {
            "number": 1,
            "title": "Source title",
            "body": "Summary\n\n" + ("x" * 120),
            "comments": [],
            "labels": ["behavior-slice"],
            "readiness": result.readiness_by_number[1],
        }
    ]
    assert result.readiness_by_number[1].selected_mode == SliceMode.BEHAVIOR


def test_planning_phase_dispatches_plan_template_to_planner(tmp_path, git_svc):
    issues = [
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 1,
            "title": "A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([_plan_output(issues)])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    asyncio.run(planning_phase(deps, issues, []))

    assert fake.calls[0].template == PromptTemplate.PLAN
    assert fake.calls[0].role == AgentRole.PLANNER
    assert fake.calls[0].stage == "plan-sandbox"


def test_planning_phase_sets_plan_work_body_from_candidate_count(tmp_path, git_svc):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
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

    assert fake.calls[0].work_body == "Creating Plan from 2 issues"


def test_planning_phase_uses_plan_override_service(tmp_path, git_svc):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([_plan_output(issues)])
    cfg = Config(plan_override=StageOverride(service="codex", effort="medium"))

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, cfg=cfg)
    asyncio.run(planning_phase(deps, issues, []))

    assert fake.calls[0].service == "codex"
    assert fake.calls[0].model == cfg.plan_override.model
    assert fake.calls[0].effort == cfg.plan_override.effort


# ── planning_phase: worktree lifecycle ──────────────────────────────────────


def test_planning_phase_removes_worktree_after_success(tmp_path, git_svc):
    issues = [
        {
            "number": 1,
            "title": "A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
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
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
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
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
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
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
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
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
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
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 3,
            "title": "Y",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([output])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == [{"number": 5, "title": "X"}]


def test_planning_phase_all_blocked_hydrates_canonical_titles_for_legacy_entries(
    tmp_path, git_svc
):
    recording = RecordingStatusDisplay()
    output = PlannerOutput(
        issues=[], blocked=[{"number": 5, "blocked_by": 3, "reason": "depends on #3"}]
    )
    issues = [
        {
            "number": 5,
            "title": "Unblock planner parsing",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
        {
            "number": 3,
            "title": "Planner blocker",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([output])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, status_display=recording)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == [{"number": 5, "title": "Unblock planner parsing"}]
    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must be removed"
    assert (
        plan_removes[0][2]
        == "All ready-for-agent issues are blocked:\n  #5: Unblock planner parsing"
    )


def test_planning_phase_all_blocked_prefers_canonical_title_over_planner_title(
    tmp_path, git_svc
):
    recording = RecordingStatusDisplay()
    output = PlannerOutput(
        issues=[],
        blocked=[
            {
                "number": 5,
                "title": "Planner supplied stale title",
                "blocked_by": 3,
                "reason": "depends on #3",
            }
        ],
    )
    issues = [
        {
            "number": 5,
            "title": "Unblock planner parsing",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
        {
            "number": 3,
            "title": "Planner blocker",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
    ]
    fake = FakeAgentRunner([output])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, status_display=recording)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == [{"number": 5, "title": "Unblock planner parsing"}]
    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must be removed"
    assert (
        plan_removes[0][2]
        == "All ready-for-agent issues are blocked:\n  #5: Unblock planner parsing"
    )


def test_planning_phase_all_blocked_accepts_concise_blocked_entries(tmp_path, git_svc):
    recording = RecordingStatusDisplay()
    blocked = [
        {"number": 5, "title": "Unblock planner parsing"},
        {"number": 6, "title": "Keep planner status tolerant"},
    ]
    output = PlannerOutput(issues=[], blocked=blocked)
    issues = [
        {
            "number": 5,
            "title": "Unblock planner parsing",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
        {
            "number": 6,
            "title": "Keep planner status tolerant",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "docs-slice"],
        },
    ]
    fake = FakeAgentRunner([output])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, status_display=recording)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == blocked
    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must be removed"
    assert (
        plan_removes[0][2] == "All ready-for-agent issues are blocked:\n"
        "  #5: Unblock planner parsing\n"
        "  #6: Keep planner status tolerant"
    )


def test_planning_phase_all_blocked_accepts_custom_blocked_entries(tmp_path, git_svc):
    recording = RecordingStatusDisplay()
    blocked = [{"number": 5, "note": "waiting on maintainer"}]
    output = PlannerOutput(issues=[], blocked=blocked)
    issues = [
        {
            "number": 5,
            "title": "Unblock planner parsing",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "behavior-slice"],
        },
        {
            "number": 6,
            "title": "Keep planner status tolerant",
            "body": "x" * 100,
            "comments": [],
            "labels": ["ready-for-agent", "docs-slice"],
        },
    ]
    fake = FakeAgentRunner([output])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, status_display=recording)
    result = asyncio.run(planning_phase(deps, issues, []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == [{"number": 5, "title": "Unblock planner parsing"}]
    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must be removed"
    assert (
        plan_removes[0][2]
        == "All ready-for-agent issues are blocked:\n  #5: Unblock planner parsing"
    )


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


def test_hydrate_planned_issues_drops_numbers_not_in_open_issues():
    plan = PlanReady(
        issues=[
            {"number": 1, "title": "A"},
            {"number": 99, "title": "Hallucinated"},
        ],
        sha="abc123",
    )
    open_issues = [
        {"number": 1, "title": "A", "body": "body of A", "comments": [], "labels": []},
    ]

    result = hydrate_planned_issues(plan, open_issues)

    assert [issue["number"] for issue in result.issues] == [1]
    assert result.issues[0]["body"] == "body of A"


def test_planning_phase_ignores_stale_planner_issue_and_keeps_valid_ones(
    tmp_path, git_svc
):
    valid_a = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    valid_b = {
        "number": 2,
        "title": "B",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    stale = {"number": 99, "title": "Stale", "body": "", "comments": []}
    fake = FakeAgentRunner([_plan_output([valid_a, stale])])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, [valid_a, valid_b], []))

    assert isinstance(result, PlanReady)
    assert [issue["number"] for issue in result.issues] == [1]


def test_hydrate_planned_issues_carries_readiness_from_sources_into_result():
    from pycastle.issue_readiness import (
        IssueReadiness,
        IssueReadinessKind,
        SliceMode,
        WellFormed,
        WellFormedBody,
    )

    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.BEHAVIOR, label="behavior-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=SliceMode.BEHAVIOR,
        kind=IssueReadinessKind.READY_AFK,
    )
    plan = PlanReady(issues=[{"number": 1, "title": "A"}], sha="abc123")
    sources = [
        {
            "number": 1,
            "title": "A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
            "readiness": readiness,
        }
    ]

    result = hydrate_planned_issues(plan, sources)

    assert result.readiness_by_number[1] is readiness


def test_hydrate_planned_issues_merges_readiness_from_plan_and_sources():
    from pycastle.issue_readiness import (
        IssueReadiness,
        IssueReadinessKind,
        SliceMode,
        WellFormed,
        WellFormedBody,
    )

    readiness_1 = IssueReadiness(
        slice_status=WellFormed(SliceMode.BEHAVIOR, label="behavior-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=SliceMode.BEHAVIOR,
        kind=IssueReadinessKind.READY_AFK,
    )
    readiness_2 = IssueReadiness(
        slice_status=WellFormed(SliceMode.DOCS, label="docs-slice"),
        body_floor_status=WellFormedBody(stripped_length=120),
        is_ready=True,
        selected_mode=SliceMode.DOCS,
        kind=IssueReadinessKind.READY_AFK,
    )
    plan = PlanReady(
        issues=[{"number": 1, "title": "A"}, {"number": 2, "title": "B"}],
        sha="abc123",
        readiness_by_number={1: readiness_1},
    )
    sources = [
        {
            "number": 1,
            "title": "A",
            "body": "x" * 100,
            "comments": [],
            "labels": ["behavior-slice"],
        },
        {
            "number": 2,
            "title": "B",
            "body": "x" * 100,
            "comments": [],
            "labels": ["docs-slice"],
            "readiness": readiness_2,
        },
    ]

    result = hydrate_planned_issues(plan, sources)

    assert result.readiness_by_number[1] is readiness_1
    assert result.readiness_by_number[2] is readiness_2


# ── Config.needs_slice_type_label ────────────────────────────────────────────


def test_config_needs_slice_type_label_defaults_to_needs_slice_type():
    from pycastle.config import Config

    assert Config().needs_slice_type_label == "needs-slice-type"


def test_planning_phase_adds_label_and_comment_for_malformed_without_flag(
    tmp_path, git_svc
):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    malformed = {
        "number": 2,
        "title": "B",
        "body": "x" * 100,
        "comments": [],
        "labels": [],
    }
    fake = FakeAgentRunner([_plan_output([well])])
    github_svc = MagicMock(spec=GithubService)

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    asyncio.run(planning_phase(deps, [well, malformed], []))

    github_svc.add_label_to_issue.assert_called_once_with(2, "needs-slice-type")
    github_svc.post_comment.assert_called_once()
    comment_body = github_svc.post_comment.call_args[0][1]
    assert "ready-for-agent" in comment_body
    assert "none" in comment_body


def test_planning_phase_applies_supplied_planning_issue_intake_label_sync_actions(
    tmp_path, git_svc
):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    malformed = {
        "number": 2,
        "title": "B",
        "body": "x" * 100,
        "comments": [],
        "labels": [],
    }
    fake = FakeAgentRunner([_plan_output([well])])
    github_svc = MagicMock(spec=GithubService)
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    prepared_issue_set = prepare_planning_issue_set([well, malformed], deps.cfg)

    asyncio.run(
        planning_phase(
            deps,
            [well, malformed],
            [],
            prepared_issue_set=prepared_issue_set,
        )
    )

    github_svc.add_label_to_issue.assert_called_once_with(2, "needs-slice-type")
    github_svc.post_comment.assert_called_once()
    github_svc.remove_label_from_issue.assert_not_called()


def test_planning_phase_removes_stale_flag_from_well_formed_issue(tmp_path, git_svc):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well1 = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice", "needs-slice-type"],
    }
    well2 = {
        "number": 2,
        "title": "B",
        "body": "x" * 100,
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


# ── planning_phase: needs-info body lifecycle ────────────────────────────────


def test_planning_phase_adds_needs_info_label_and_comment_for_short_body(
    tmp_path, git_svc
):
    from unittest.mock import MagicMock
    from pycastle.services.github_service import GithubService

    well = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    short_body = {
        "number": 2,
        "title": "B",
        "body": "too short",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([_plan_output([well])])
    github_svc = MagicMock(spec=GithubService)

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    asyncio.run(planning_phase(deps, [well, short_body], []))

    github_svc.add_label_to_issue.assert_called_once_with(2, "needs-info")
    github_svc.post_comment.assert_called_once()
    comment_body = github_svc.post_comment.call_args[0][1]
    assert "too short" in comment_body
    assert "needs-info" in comment_body


def test_planning_phase_adds_both_marker_labels_and_comments_for_doubly_blocked_issue(
    tmp_path, git_svc
):
    from unittest.mock import MagicMock, call
    from pycastle.services.github_service import GithubService

    well = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["behavior-slice"],
    }
    doubly_blocked = {
        "number": 2,
        "title": "B",
        "body": "short",
        "comments": [],
        "labels": [],
    }
    fake = FakeAgentRunner([_plan_output([well])])
    github_svc = MagicMock(spec=GithubService)

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, github_svc=github_svc)
    asyncio.run(planning_phase(deps, [well, doubly_blocked], []))

    assert github_svc.add_label_to_issue.call_args_list == [
        call(2, "needs-info"),
        call(2, "needs-slice-type"),
    ]
    assert github_svc.post_comment.call_count == 2
    comment_bodies = [args[0][1] for args in github_svc.post_comment.call_args_list]
    assert any("body is too short" in body for body in comment_bodies)
    assert any(
        "missing exactly one slice-mode label" in body for body in comment_bodies
    )


def test_planning_phase_single_issue_with_short_body_excluded_from_short_circuit(
    tmp_path, git_svc
):
    short_body = {
        "number": 1,
        "title": "A",
        "body": "too short",
        "comments": [],
        "labels": ["behavior-slice"],
    }
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, [short_body], []))

    assert isinstance(result, AllBlocked)
    assert len(fake.calls) == 0


def test_planning_phase_single_malformed_issue_skips_planner_and_returns_all_blocked(
    tmp_path, git_svc
):
    malformed = {
        "number": 1,
        "title": "A",
        "body": "short",
        "comments": [],
        "labels": [],
    }
    fake = FakeAgentRunner([])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc)
    result = asyncio.run(planning_phase(deps, [malformed], []))

    assert isinstance(result, AllBlocked)
    assert result.blocked == []
    assert len(fake.calls) == 0, (
        "Planner must not be called when no readiness candidates exist"
    )
    git_svc.create_worktree.assert_not_called()


def test_planning_phase_all_blocked_summary_precedes_planner_blocked_lines(
    tmp_path, git_svc
):
    recording = RecordingStatusDisplay()
    missing_slice = {
        "number": 1,
        "title": "A",
        "body": "x" * 100,
        "comments": [],
        "labels": ["ready-for-agent"],
    }
    blocked_issue = {
        "number": 2,
        "title": "B",
        "body": "x" * 100,
        "comments": [],
        "labels": ["ready-for-agent", "behavior-slice"],
    }
    blocked_issue_two = {
        "number": 3,
        "title": "C",
        "body": "x" * 100,
        "comments": [],
        "labels": ["ready-for-agent", "docs-slice"],
    }
    blocked = [{"number": 2, "blocked_by": 9, "reason": "depends on #9"}]
    fake = FakeAgentRunner([PlannerOutput(issues=[], blocked=blocked)])

    deps = _make_deps(tmp_path, fake, git_svc=git_svc, status_display=recording)
    asyncio.run(
        planning_phase(deps, [missing_slice, blocked_issue, blocked_issue_two], [])
    )

    plan_removes = [c for c in recording.calls if c[0] == "remove" and c[1] == "Plan"]
    assert plan_removes, "Plan row must be removed"
    assert (
        plan_removes[0][2] == "All ready-for-agent issues are blocked:\n"
        "Planning blockers: 1 missing exactly one slice-mode label.\n"
        "  #2: B"
    )


# ── planning_phase: in-flight preflight gate ───────────────────────────────


def test_planning_phase_in_flight_returns_safe_sha_without_calling_planner(
    tmp_path, git_svc
):
    from pycastle.iteration.preflight import PreflightReady

    call_count = 0

    class _TrackingCache:
        async def get_safe_sha(self, deps):
            nonlocal call_count
            call_count += 1
            return PreflightReady(sha="safe-sha-123")

    issues = [{"number": 1, "title": "A", "body": "", "comments": []}]
    fake = FakeAgentRunner([])
    deps = _make_deps(tmp_path, fake, git_svc=git_svc, preflight_cache=_TrackingCache())
    result = asyncio.run(planning_phase(deps, issues, [], in_flight=issues))

    assert isinstance(result, PlanReady)
    assert result.sha == "safe-sha-123", (
        "in-flight path must return the safe SHA from the preflight gate"
    )
    assert call_count == 1, "get_safe_sha must be called on in-flight planning path"
    assert len(fake.calls) == 0, "Planner must not be called on in-flight planning path"


def test_planning_phase_in_flight_returns_preflight_afk_before_resuming(
    tmp_path, git_svc
):
    from pycastle.iteration.preflight import PreflightAFK

    issues = [{"number": 1, "title": "A", "body": "", "comments": []}]
    fake = FakeAgentRunner([])
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        preflight_cache=StubPreflightCache(
            PreflightAFK(sha="safe-sha-123", issue_number=181)
        ),
    )

    result = asyncio.run(planning_phase(deps, issues, [], in_flight=issues))

    assert result == PreflightAFK(sha="safe-sha-123", issue_number=181)
    assert len(fake.calls) == 0, "Planner must not be called on in-flight AFK path"


def test_planning_phase_in_flight_returns_preflight_hitl_before_resuming(
    tmp_path, git_svc
):
    from pycastle.iteration.preflight import PreflightHITL

    issues = [{"number": 1, "title": "A", "body": "", "comments": []}]
    fake = FakeAgentRunner([])
    deps = _make_deps(
        tmp_path,
        fake,
        git_svc=git_svc,
        preflight_cache=StubPreflightCache(
            PreflightHITL(sha="safe-sha-123", issue_number=182)
        ),
    )

    result = asyncio.run(planning_phase(deps, issues, [], in_flight=issues))

    assert result == PreflightHITL(sha="safe-sha-123", issue_number=182)
    assert len(fake.calls) == 0, "Planner must not be called on in-flight HITL path"
