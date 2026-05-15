from pycastle.config import Config
from pycastle.prompt_pipeline import PromptTemplate
from pycastle.slice_classifier import (
    Malformed,
    SliceMode,
    WellFormed,
    classify_slice,
    slice_labels,
)

_cfg = Config()


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
