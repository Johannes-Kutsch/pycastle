"""Tests for _candidate_namespace builder and _parse_candidate_namespace parser seam."""

import pytest

from pycastle.iteration.improve import (
    _candidate_namespace,
    _CandidateNamespaceParseError,
    _parse_candidate_namespace,
)

# ── 1. Golden format ──────────────────────────────────────────────────────────


def test_candidate_namespace_zero_is_canonical_string() -> None:
    assert _candidate_namespace(0) == "candidate/0"


# ── 2. Round-trip: parse(_candidate_namespace(n)) == n ───────────────────────


@pytest.mark.parametrize("n", [0, 1, 2, 9, 10, 99, 100])
def test_round_trip(n: int) -> None:
    assert _parse_candidate_namespace(_candidate_namespace(n)) == n


# ── 3. Valid parse — positive index ──────────────────────────────────────────


def test_parse_returns_index_for_small_positive() -> None:
    assert _parse_candidate_namespace("candidate/3") == 3


def test_parse_returns_index_for_large_value() -> None:
    assert _parse_candidate_namespace("candidate/500") == 500


# ── 4. Malformed inputs raise _CandidateNamespaceParseError ──────────────────


def test_parse_raises_on_empty_string() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("")


def test_parse_raises_on_leading_slash() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("/candidate/0")


def test_parse_raises_on_trailing_slash() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("candidate/0/")


def test_parse_raises_on_absolute_path_form() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("/0")


def test_parse_raises_on_dotdot_segment() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("../0")


def test_parse_raises_on_dotdot_as_second_segment() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("candidate/..")


def test_parse_raises_on_wrong_first_segment() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("other/0")


def test_parse_raises_on_only_prefix_no_index() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("candidate")


def test_parse_raises_on_non_integer_index() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("candidate/abc")


def test_parse_raises_on_negative_integer_index() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("candidate/-1")


# ── 5. Error message carries the offending input ─────────────────────────────


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "/candidate/0",
        "candidate/0/",
        "../0",
        "other/0",
        "candidate",
        "candidate/abc",
        "candidate/-1",
    ],
)
def test_error_message_contains_offending_input(bad_input: str) -> None:
    with pytest.raises(_CandidateNamespaceParseError, match=repr(bad_input)):
        _parse_candidate_namespace(bad_input)


# ── 6. Error is not a bare built-in exception ─────────────────────────────────


def test_error_is_not_bare_value_error() -> None:
    assert _CandidateNamespaceParseError is not ValueError


def test_error_is_not_bare_index_error() -> None:
    assert _CandidateNamespaceParseError is not IndexError


def test_raised_error_is_candidate_namespace_parse_error() -> None:
    with pytest.raises(_CandidateNamespaceParseError):
        _parse_candidate_namespace("bad")
