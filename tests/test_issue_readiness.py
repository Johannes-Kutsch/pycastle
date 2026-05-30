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
