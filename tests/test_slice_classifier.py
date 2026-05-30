from pycastle.config import Config
from pycastle.issue_readiness import BODY_FLOOR
from pycastle.prompts.pipeline import PromptTemplate
from pycastle.agents.classifier import (
    IssueReadiness,
    Malformed,
    MalformedBody,
    WellFormedBody,
    SliceMode,
    classify_issue_readiness,
    WellFormed,
    classify_slice,
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


def test_classify_slice_refactor():
    issue = {"number": 1, "labels": ["refactor-slice"]}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, WellFormed)
    assert result.mode is SliceMode.REFACTOR
    assert result.mode.display_name == "refactor"
    assert result.mode.template is PromptTemplate.IMPLEMENT_REFACTOR


def test_classify_slice_behavior():
    issue = {"number": 1, "labels": ["behavior-slice"]}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, WellFormed)
    assert result.mode is SliceMode.BEHAVIOR
    assert result.mode.display_name == "behavior"
    assert result.mode.template is PromptTemplate.IMPLEMENT_BEHAVIOR


def test_classify_slice_docs():
    issue = {"number": 1, "labels": ["docs-slice"]}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, WellFormed)
    assert result.mode is SliceMode.DOCS
    assert result.mode.display_name == "docs"
    assert result.mode.template is PromptTemplate.IMPLEMENT_DOCS


def test_classify_slice_zero_labels():
    issue = {"number": 1, "labels": []}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, Malformed)
    assert result.found == []


def test_classify_slice_two_labels():
    issue = {"number": 1, "labels": ["refactor-slice", "behavior-slice"]}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, Malformed)
    assert set(result.found) == {"refactor-slice", "behavior-slice"}


def test_classify_slice_three_labels():
    issue = {"number": 1, "labels": ["refactor-slice", "behavior-slice", "docs-slice"]}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, Malformed)
    assert set(result.found) == {"refactor-slice", "behavior-slice", "docs-slice"}


def test_classify_slice_missing_labels_key():
    issue = {"number": 1}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, Malformed)
    assert result.found == []


def test_classify_slice_non_slice_labels_mixed_in():
    issue = {"number": 1, "labels": ["bug", "ready-for-agent", "behavior-slice"]}
    result = classify_slice(issue, _cfg)
    assert isinstance(result, WellFormed)
    assert result.mode is SliceMode.BEHAVIOR


def test_classify_slice_custom_renamed_label():
    cfg = Config(refactor_slice_label="custom-refactor")
    issue = {"number": 1, "labels": ["custom-refactor"]}
    result = classify_slice(issue, cfg)
    assert isinstance(result, WellFormed)
    assert result.mode is SliceMode.REFACTOR


def test_classify_slice_old_label_not_matched_after_rename():
    cfg = Config(refactor_slice_label="custom-refactor")
    issue = {"number": 1, "labels": ["refactor-slice"]}
    result = classify_slice(issue, cfg)
    assert isinstance(result, Malformed)


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
