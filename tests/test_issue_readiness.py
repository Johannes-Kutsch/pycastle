from pycastle.config import Config
from pycastle.issue_readiness import (
    AFKBlockedOutcome,
    AFKReadyOutcome,
    BODY_FLOOR,
    BlockedIssueOutcome,
    IssueReadiness,
    IssueReadinessKind,
    Malformed,
    MalformedBody,
    MarkerLabelDecision,
    ReadyIssueOutcome,
    SliceMode,
    WellFormed,
    WellFormedBody,
    classify_issue_readiness,
    evaluate_issue_afk_readiness,
    issue_readiness_error_for_issue,
    ready_slice_outcome_for_issue,
    resolve_issue_readiness,
    selected_mode_for_issue,
)

_cfg = Config()


def test_evaluate_issue_afk_readiness_returns_ready_outcome_for_single_valid_slice_mode():
    issue = {
        "number": 1,
        "labels": ["bug", "behavior-slice", "ready-for-agent"],
        "body": "x" * BODY_FLOOR,
    }

    result = evaluate_issue_afk_readiness(issue, _cfg)

    assert result == AFKReadyOutcome(
        slice_mode_display_name="behavior",
        implement_template=SliceMode.BEHAVIOR.template,
    )


def test_evaluate_issue_afk_readiness_returns_blocked_outcome_for_missing_or_multiple_slice_modes():
    missing = {
        "number": 1,
        "labels": ["bug"],
        "body": "x" * BODY_FLOOR,
    }
    multiple = {
        "number": 2,
        "labels": ["docs-slice", "behavior-slice", "bug"],
        "body": "x" * BODY_FLOOR,
    }

    missing_result = evaluate_issue_afk_readiness(missing, _cfg)
    multiple_result = evaluate_issue_afk_readiness(multiple, _cfg)

    assert missing_result == AFKBlockedOutcome(
        current_slice_labels=(),
        marker_decisions=(MarkerLabelDecision(label_name="needs-slice-type"),),
        stripped_body_length=BODY_FLOOR,
        body_floor=BODY_FLOOR,
        has_invalid_slice_mode=True,
    )
    assert multiple_result == AFKBlockedOutcome(
        current_slice_labels=("docs-slice", "behavior-slice"),
        marker_decisions=(
            MarkerLabelDecision(label_name="needs-slice-type", intent="add"),
        ),
        stripped_body_length=BODY_FLOOR,
        body_floor=BODY_FLOOR,
        has_invalid_slice_mode=True,
    )


def test_evaluate_issue_afk_readiness_honours_configured_slice_and_marker_labels():
    cfg = Config(
        refactor_slice_label="custom-refactor",
        needs_slice_type_label="needs-mode",
        needs_info_label="awaiting-details",
    )
    issue = {
        "number": 3,
        "labels": ["bug", "custom-refactor", "docs-slice"],
        "body": "short",
    }

    result = evaluate_issue_afk_readiness(issue, cfg)

    assert result == AFKBlockedOutcome(
        current_slice_labels=("custom-refactor", "docs-slice"),
        marker_decisions=(
            MarkerLabelDecision(label_name="needs-mode", intent="add"),
            MarkerLabelDecision(label_name="awaiting-details", intent="add"),
        ),
        stripped_body_length=5,
        body_floor=BODY_FLOOR,
        has_invalid_slice_mode=True,
        has_short_body=True,
    )


def test_evaluate_issue_afk_readiness_reports_both_blocked_facts_in_one_outcome():
    issue = {
        "number": 4,
        "labels": ["refactor-slice", "behavior-slice", "bug"],
        "body": "@-",
    }

    result = evaluate_issue_afk_readiness(issue, _cfg)

    assert result == AFKBlockedOutcome(
        current_slice_labels=("refactor-slice", "behavior-slice"),
        marker_decisions=(
            MarkerLabelDecision(label_name="needs-slice-type", intent="add"),
            MarkerLabelDecision(label_name="needs-info", intent="add"),
        ),
        stripped_body_length=2,
        body_floor=BODY_FLOOR,
        has_invalid_slice_mode=True,
        has_short_body=True,
    )


def test_evaluate_issue_afk_readiness_returns_ready_outcome_for_carried_readiness_without_ready_payload():
    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.DOCS, label="docs-slice"),
        body_floor_status=WellFormedBody(stripped_length=BODY_FLOOR),
        is_ready=True,
        selected_mode=SliceMode.DOCS,
        kind=IssueReadinessKind.READY_AFK,
    )
    issue = {
        "number": 5,
        "labels": ["behavior-slice"],
        "body": "short",
        "readiness": readiness,
    }

    result = evaluate_issue_afk_readiness(issue, _cfg)

    assert result == AFKReadyOutcome(
        slice_mode_display_name="docs",
        implement_template=SliceMode.DOCS.template,
    )


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


def test_evaluate_issue_afk_readiness_applies_body_floor_at_public_interface():
    empty = evaluate_issue_afk_readiness({"number": 1, "labels": ["docs-slice"]}, _cfg)
    whitespace = evaluate_issue_afk_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": " \n\t "},
        _cfg,
    )
    at_marker = evaluate_issue_afk_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": "@-"},
        _cfg,
    )
    below_floor = evaluate_issue_afk_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": "x" * (BODY_FLOOR - 1)},
        _cfg,
    )
    at_floor = evaluate_issue_afk_readiness(
        {"number": 1, "labels": ["docs-slice"], "body": "x" * BODY_FLOOR},
        _cfg,
    )

    for result, expected_length in [
        (empty, 0),
        (whitespace, 0),
        (at_marker, 2),
        (below_floor, BODY_FLOOR - 1),
    ]:
        assert result == AFKBlockedOutcome(
            current_slice_labels=(),
            marker_decisions=(MarkerLabelDecision(label_name="needs-info"),),
            stripped_body_length=expected_length,
            body_floor=BODY_FLOOR,
            has_short_body=True,
        )
    assert at_floor == AFKReadyOutcome(
        slice_mode_display_name="docs",
        implement_template=SliceMode.DOCS.template,
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


def test_classify_issue_readiness_kind_is_ready_afk_for_well_formed_issue():
    issue = {"number": 1, "labels": ["behavior-slice"], "body": "x" * BODY_FLOOR}

    result = classify_issue_readiness(issue, _cfg)

    assert result.kind == IssueReadinessKind.READY_AFK
    assert result.is_ready is True
    assert result.selected_mode == SliceMode.BEHAVIOR
    assert result.ready == ReadyIssueOutcome(
        display_name="behavior",
        template=SliceMode.BEHAVIOR.template,
    )
    assert result.blocked is None


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


def test_classify_issue_readiness_blocked_outcome_carries_slice_and_body_facts():
    cfg = Config(refactor_slice_label="custom-refactor")
    issue = {
        "number": 1,
        "labels": ["custom-refactor", "behavior-slice"],
        "body": "short",
    }

    result = classify_issue_readiness(issue, cfg)

    assert result.blocked == BlockedIssueOutcome(
        slice_status=Malformed(
            found=["custom-refactor", "behavior-slice"],
            configured=frozenset({"custom-refactor", "behavior-slice", "docs-slice"}),
        ),
        body_floor_status=MalformedBody(
            stripped_length=5,
            body_floor=BODY_FLOOR,
        ),
    )
    assert result.ready is None


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


def test_resolve_issue_readiness_returns_carried_readiness_unchanged():
    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.DOCS, label="docs-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=SliceMode.DOCS,
        kind=IssueReadinessKind.READY_AFK,
    )
    issue = {
        "number": 1,
        "labels": ["behavior-slice"],
        "body": "short",
        "readiness": readiness,
    }

    assert resolve_issue_readiness(issue, _cfg) is readiness


def test_resolve_issue_readiness_classifies_when_issue_has_no_carried_readiness():
    issue = {"number": 1, "labels": ["refactor-slice"], "body": "x" * BODY_FLOOR}

    assert resolve_issue_readiness(issue, _cfg) == classify_issue_readiness(issue, _cfg)


def test_issue_readiness_error_for_issue_reports_missing_slice_mode():
    assert issue_readiness_error_for_issue(
        caller="Preflight Issue Agent",
        issue={"number": 1, "labels": ["bug"], "body": "x" * BODY_FLOOR},
        cfg=_cfg,
    ) == (
        "Preflight Issue Agent filed issue #1 on the AFK branch without exactly "
        "one slice-mode label — got labels=['bug']. Expected exactly one of "
        "['behavior-slice', 'docs-slice', 'refactor-slice']."
    )


def test_issue_readiness_error_for_issue_reports_short_body():
    assert issue_readiness_error_for_issue(
        caller="Host-Check Reporter",
        issue={"number": 1, "labels": ["bug", "behavior-slice"], "body": "short"},
        cfg=_cfg,
    ) == (
        "Host-Check Reporter filed issue #1 whose body is below the minimum "
        "length floor — body too short to be valid."
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


def test_selected_mode_for_issue_falls_back_to_carried_slice_when_ready_payload_exists():
    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.BEHAVIOR, label="behavior-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=None,
        ready=ReadyIssueOutcome(
            display_name="behavior",
            template=SliceMode.BEHAVIOR.template,
        ),
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


# ── ready_slice_outcome_for_issue ────────────────────────────────────────────


def test_ready_slice_outcome_for_issue_prefers_carried_ready_payload_over_fallback_mode():
    ready = ReadyIssueOutcome(
        display_name="docs",
        template=SliceMode.DOCS.template,
    )
    readiness = IssueReadiness(
        slice_status=WellFormed(SliceMode.REFACTOR, label="refactor-slice"),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=True,
        selected_mode=None,
        ready=ready,
        kind=IssueReadinessKind.READY_AFK,
    )
    issue = {
        "number": 1,
        "labels": ["behavior-slice"],
        "body": "x" * 100,
        "readiness": readiness,
    }

    assert ready_slice_outcome_for_issue(issue, _cfg) is ready


def test_ready_slice_outcome_for_issue_returns_none_when_no_ready_result_can_be_constructed():
    readiness = IssueReadiness(
        slice_status=Malformed(found=[]),
        body_floor_status=WellFormedBody(stripped_length=100),
        is_ready=False,
        selected_mode=None,
        kind=IssueReadinessKind.MISSING_SLICE_MODE,
    )
    issue = {"number": 1, "labels": [], "body": "x" * 100, "readiness": readiness}

    assert ready_slice_outcome_for_issue(issue, _cfg) is None
