from pycastle.config import Config
from pycastle.issue_readiness import (
    BODY_FLOOR,
    IssueReadiness,
    IssueReadinessKind,
    Malformed,
    MalformedBody,
    SliceMode,
    WellFormed,
    WellFormedBody,
    classify_issue_readiness,
    classify_issues,
    partition_classified_issues,
    selected_mode_for_issue,
    slice_labels,
)

_cfg = Config()


def test_classify_issue_readiness_ready_selects_matching_slice_mode():
    issue = {
        "number": 1,
        "labels": ["behavior-slice"],
        "body": "x" * 100,
    }

    result = classify_issue_readiness(issue, _cfg)

    assert result == IssueReadiness(
        slice_status=WellFormed(
            SliceMode.BEHAVIOR,
            label="behavior-slice",
        ),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=SliceMode.BEHAVIOR,
    )


def test_classify_issue_readiness_slice_label_failures_expose_found_labels():
    missing = classify_issue_readiness({"number": 1, "body": "x" * 100}, _cfg)
    multiple = classify_issue_readiness(
        {
            "number": 1,
            "labels": ["docs-slice", "behavior-slice", "bug"],
            "body": "x" * 100,
        },
        _cfg,
    )

    assert missing == IssueReadiness(
        slice_status=Malformed(
            found=[],
            configured=frozenset({"refactor-slice", "behavior-slice", "docs-slice"}),
        ),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=False,
        selected_mode=None,
    )
    assert multiple == IssueReadiness(
        slice_status=Malformed(
            found=["docs-slice", "behavior-slice"],
            configured=frozenset({"refactor-slice", "behavior-slice", "docs-slice"}),
        ),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=False,
        selected_mode=None,
    )


def test_classify_issue_readiness_body_floor_status_handles_edge_cases():
    empty = classify_issue_readiness({"number": 1, "labels": ["docs-slice"]}, _cfg)
    whitespace = classify_issue_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": " \n\t "},
        _cfg,
    )
    at_marker = classify_issue_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": "@-"},
        _cfg,
    )
    below_floor = classify_issue_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": "x" * (BODY_FLOOR - 1)},
        _cfg,
    )
    at_floor = classify_issue_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": "x" * BODY_FLOOR},
        _cfg,
    )

    assert empty.body_floor_status == MalformedBody(
        stripped_length=0,
        body_floor=BODY_FLOOR,
    )
    assert whitespace.body_floor_status == MalformedBody(
        stripped_length=0,
        body_floor=BODY_FLOOR,
    )
    assert at_marker.body_floor_status == MalformedBody(
        stripped_length=2,
        body_floor=BODY_FLOOR,
    )
    assert below_floor.body_floor_status == MalformedBody(
        stripped_length=BODY_FLOOR - 1,
        body_floor=BODY_FLOOR,
    )
    assert at_floor.body_floor_status == WellFormedBody(
        stripped_length=BODY_FLOOR,
        body_floor=BODY_FLOOR,
    )


def test_classify_issue_readiness_reports_slice_and_body_failures_together():
    result = classify_issue_readiness(
        {
            "number": 1,
            "labels": ["refactor-slice", "behavior-slice", "bug"],
            "body": "@-",
        },
        _cfg,
    )

    assert result == IssueReadiness(
        slice_status=Malformed(
            found=["refactor-slice", "behavior-slice"],
            configured=frozenset({"refactor-slice", "behavior-slice", "docs-slice"}),
        ),
        body_floor_status=MalformedBody(
            stripped_length=2,
            body_floor=BODY_FLOOR,
        ),
        is_ready=False,
        selected_mode=None,
    )


def test_classify_issue_readiness_ignores_unrelated_labels_and_honors_renames():
    cfg = Config(refactor_slice_label="custom-refactor")

    ready = classify_issue_readiness(
        {
            "number": 1,
            "labels": ["bug", "ready-for-agent", "custom-refactor"],
            "body": "x" * BODY_FLOOR,
        },
        cfg,
    )
    renamed_away = classify_issue_readiness(
        {
            "number": 1,
            "labels": ["bug", "ready-for-agent", "refactor-slice"],
            "body": "x" * BODY_FLOOR,
        },
        cfg,
    )

    assert ready == IssueReadiness(
        slice_status=WellFormed(
            SliceMode.REFACTOR,
            label="custom-refactor",
        ),
        body_floor_status=WellFormedBody(
            stripped_length=BODY_FLOOR,
            body_floor=BODY_FLOOR,
        ),
        is_ready=True,
        selected_mode=SliceMode.REFACTOR,
    )
    assert renamed_away == IssueReadiness(
        slice_status=Malformed(
            found=[],
            configured=frozenset({"custom-refactor", "behavior-slice", "docs-slice"}),
        ),
        body_floor_status=WellFormedBody(
            stripped_length=BODY_FLOOR,
            body_floor=BODY_FLOOR,
        ),
        is_ready=False,
        selected_mode=None,
    )


def test_slice_labels_returns_three_configured_strings():
    result = slice_labels(_cfg)
    assert result == frozenset({"refactor-slice", "behavior-slice", "docs-slice"})


def test_slice_labels_reflects_custom_config():
    cfg = Config(
        refactor_slice_label="r-slice",
        behavior_slice_label="b-slice",
        docs_slice_label="d-slice",
    )
    assert slice_labels(cfg) == frozenset({"r-slice", "b-slice", "d-slice"})


def test_classify_issue_readiness_kind_is_ready_afk_for_well_formed_issue():
    issue = {"number": 1, "labels": ["behavior-slice"], "body": "x" * BODY_FLOOR}

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.READY_AFK
    assert result.is_ready is True
    assert result.selected_mode == SliceMode.BEHAVIOR


def test_classify_issue_readiness_kind_is_missing_slice_mode_when_no_slice_label():
    issue = {"number": 1, "labels": [], "body": "x" * BODY_FLOOR}

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.MISSING_SLICE_MODE
    assert result.is_ready is False


def test_classify_issue_readiness_kind_is_multiple_slice_modes_when_two_slice_labels():
    issue = {
        "number": 1,
        "labels": ["behavior-slice", "docs-slice"],
        "body": "x" * BODY_FLOOR,
    }

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.MULTIPLE_SLICE_MODES
    assert result.is_ready is False


def test_classify_issue_readiness_kind_is_short_body_when_body_below_floor():
    issue = {"number": 1, "labels": ["refactor-slice"], "body": "x" * (BODY_FLOOR - 1)}

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.SHORT_BODY
    assert result.is_ready is False


def test_classify_issue_readiness_kind_is_malformed_when_both_slice_and_body_fail():
    issue = {
        "number": 1,
        "labels": ["behavior-slice", "docs-slice"],
        "body": "x" * (BODY_FLOOR - 1),
    }

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.MALFORMED
    assert result.is_ready is False


def test_classify_issue_readiness_kind_is_hitl_exempt_when_hitl_label_present():
    issue = {
        "number": 1,
        "labels": ["ready-for-human"],
        "body": "x" * (BODY_FLOOR - 1),
    }

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.HITL_EXEMPT
    assert result.is_ready is False
    assert result.is_hitl_exempt is True
    assert result.hitl_label == "ready-for-human"


def test_classify_issue_readiness_hitl_exempt_takes_priority_over_well_formed():
    issue = {
        "number": 1,
        "labels": ["ready-for-human", "behavior-slice"],
        "body": "x" * BODY_FLOOR,
    }

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.HITL_EXEMPT
    assert result.is_ready is False
    assert result.is_hitl_exempt is True


def test_classify_issue_readiness_hitl_label_absent_when_issue_has_no_hitl_label():
    issue = {"number": 1, "labels": ["behavior-slice"], "body": "x" * BODY_FLOOR}

    result = classify_issue_readiness(issue, _cfg)

    assert result.hitl_label is None
    assert result.is_hitl_exempt is False


def test_classify_issue_readiness_custom_hitl_label_is_honoured():
    cfg = Config(hitl_label="needs-human")
    issue = {
        "number": 1,
        "labels": ["needs-human"],
        "body": "x" * (BODY_FLOOR - 1),
    }

    result = classify_issue_readiness(issue, cfg)

    assert result.kind == IssueReadinessKind.HITL_EXEMPT
    assert result.hitl_label == "needs-human"
    assert result.is_hitl_exempt is True


# ── partition_classified_issues ──────────────────────────────────────────────


def test_partition_classified_issues_ready_issue_in_ready_and_both_well_formed_lists():
    issue = {"number": 1, "labels": ["behavior-slice"], "body": "x" * BODY_FLOOR}
    classified = classify_issues([issue], _cfg)

    partition = partition_classified_issues(classified)

    assert partition.ready == [issue]
    assert partition.slice_well_formed == [issue]
    assert partition.body_well_formed == [issue]
    assert partition.slice_malformed == []
    assert partition.body_malformed == []


def test_partition_classified_issues_missing_slice_excluded_from_ready_and_slice_well_formed():
    issue = {"number": 1, "labels": [], "body": "x" * BODY_FLOOR}
    classified = classify_issues([issue], _cfg)

    partition = partition_classified_issues(classified)

    assert partition.ready == []
    assert partition.slice_malformed == [issue]
    assert partition.slice_well_formed == []
    assert partition.body_well_formed == [issue]
    assert partition.body_malformed == []


def test_partition_classified_issues_short_body_excluded_from_ready_and_body_well_formed():
    issue = {"number": 1, "labels": ["behavior-slice"], "body": "short"}
    classified = classify_issues([issue], _cfg)

    partition = partition_classified_issues(classified)

    assert partition.ready == []
    assert partition.slice_well_formed == [issue]
    assert partition.slice_malformed == []
    assert partition.body_malformed == [issue]
    assert partition.body_well_formed == []


def test_partition_classified_issues_issue_malformed_in_both_dimensions_appears_in_both_malformed_lists():
    issue = {"number": 1, "labels": [], "body": "short"}
    classified = classify_issues([issue], _cfg)

    partition = partition_classified_issues(classified)

    assert partition.ready == []
    assert partition.slice_malformed == [issue]
    assert partition.body_malformed == [issue]
    assert partition.slice_well_formed == []
    assert partition.body_well_formed == []


def test_partition_classified_issues_empty_list_returns_all_empty_partitions():
    partition = partition_classified_issues([])

    assert partition.ready == []
    assert partition.slice_well_formed == []
    assert partition.slice_malformed == []
    assert partition.body_well_formed == []
    assert partition.body_malformed == []


def test_partition_classified_issues_hitl_exempt_excluded_from_ready():
    issue = {
        "number": 1,
        "labels": ["ready-for-human", "behavior-slice"],
        "body": "x" * BODY_FLOOR,
    }
    classified = classify_issues([issue], _cfg)

    partition = partition_classified_issues(classified)

    assert partition.ready == []
    assert partition.slice_well_formed == [issue]
    assert partition.body_well_formed == [issue]


def test_partition_classified_issues_slice_and_body_partitions_cover_all_issues():
    issues = [
        {"number": 1, "labels": ["behavior-slice"], "body": "x" * BODY_FLOOR},
        {"number": 2, "labels": [], "body": "short"},
        {"number": 3, "labels": ["refactor-slice"], "body": "short"},
    ]
    classified = classify_issues(issues, _cfg)

    partition = partition_classified_issues(classified)

    assert len(partition.slice_well_formed) + len(partition.slice_malformed) == len(
        issues
    )
    assert len(partition.body_well_formed) + len(partition.body_malformed) == len(
        issues
    )


# ── selected_mode_for_issue ──────────────────────────────────────────────────


def test_selected_mode_for_issue_returns_carried_selected_mode():
    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.BEHAVIOR, label="behavior-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=SliceMode.BEHAVIOR,
        kind=IssueReadinessKind.READY_AFK,
    )
    issue = {"number": 1, "labels": [], "body": "", "readiness": readiness}

    assert selected_mode_for_issue(issue, _cfg) == SliceMode.BEHAVIOR


def test_selected_mode_for_issue_carried_mode_takes_precedence_over_labels():
    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.REFACTOR, label="refactor-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=SliceMode.REFACTOR,
        kind=IssueReadinessKind.READY_AFK,
    )
    # Labels say docs-slice but carried readiness says refactor
    issue = {
        "number": 1,
        "labels": ["docs-slice"],
        "body": "x" * 100,
        "readiness": readiness,
    }

    assert selected_mode_for_issue(issue, _cfg) == SliceMode.REFACTOR


def test_selected_mode_for_issue_returns_none_for_carried_malformed_readiness():
    readiness = IssueReadiness(
        slice_status=Malformed(found=[]),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=False,
        selected_mode=None,
        kind=IssueReadinessKind.MISSING_SLICE_MODE,
    )
    issue = {"number": 1, "labels": [], "body": "", "readiness": readiness}

    assert selected_mode_for_issue(issue, _cfg) is None


def test_selected_mode_for_issue_falls_back_to_label_classification_without_readiness():
    issue = {"number": 1, "labels": ["refactor-slice"], "body": "x"}

    assert selected_mode_for_issue(issue, _cfg) == SliceMode.REFACTOR


def test_selected_mode_for_issue_fallback_covers_all_slice_mode_labels():
    for label, expected in [
        ("behavior-slice", SliceMode.BEHAVIOR),
        ("refactor-slice", SliceMode.REFACTOR),
        ("docs-slice", SliceMode.DOCS),
    ]:
        issue = {"number": 1, "labels": [label], "body": "x"}
        assert selected_mode_for_issue(issue, _cfg) == expected


def test_selected_mode_for_issue_returns_none_for_missing_labels_without_readiness():
    issue = {"number": 1, "labels": [], "body": "x"}

    assert selected_mode_for_issue(issue, _cfg) is None


def test_selected_mode_for_issue_returns_none_for_multiple_labels_without_readiness():
    issue = {"number": 1, "labels": ["behavior-slice", "refactor-slice"], "body": "x"}

    assert selected_mode_for_issue(issue, _cfg) is None
