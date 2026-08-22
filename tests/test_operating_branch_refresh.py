import pytest

from pycastle.services._operating_branch_refresh import (
    AlreadyCurrent,
    FastForwardLocalRef,
    NoUpstreamYet,
    OperatingBranchDiverged,
    classify_ref_relation,
)


@pytest.mark.parametrize(
    "ancestry", ["equal", "local_ahead", "remote_ahead", "diverged", None]
)
def test_no_upstream_whenever_upstream_ref_absent(ancestry):
    decision = classify_ref_relation(upstream_ref_exists=False, ancestry=ancestry)

    assert decision == NoUpstreamYet()


@pytest.mark.parametrize("ancestry", ["equal", "local_ahead"])
def test_already_current_when_refs_equal_or_local_contains_remote_tip(ancestry):
    decision = classify_ref_relation(upstream_ref_exists=True, ancestry=ancestry)

    assert decision == AlreadyCurrent()


def test_fast_forward_when_remote_is_ahead():
    decision = classify_ref_relation(upstream_ref_exists=True, ancestry="remote_ahead")

    assert decision == FastForwardLocalRef()


def test_diverged_when_each_side_has_exclusive_commits():
    decision = classify_ref_relation(upstream_ref_exists=True, ancestry="diverged")

    assert decision == OperatingBranchDiverged()


def test_diverged_when_upstream_present_but_ancestry_unknown():
    decision = classify_ref_relation(upstream_ref_exists=True, ancestry=None)

    assert decision == OperatingBranchDiverged()
