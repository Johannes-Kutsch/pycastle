from pathlib import Path

from pycastle.config import Config
from pycastle.iteration.branch_resolution import (
    BranchFacts,
    BranchSetupPlan,
    DevBranchMissing,
    Fetch,
    PushUpstream,
    Seed,
    find_checked_out_worktrees,
    resolve_branch_setup,
)


def _facts(
    *,
    dev_branch_on_origin: bool = True,
    working_branch_on_local: bool = False,
    working_branch_on_origin: bool = False,
) -> BranchFacts:
    return BranchFacts(
        dev_branch_on_origin=dev_branch_on_origin,
        working_branch_on_local=working_branch_on_local,
        working_branch_on_origin=working_branch_on_origin,
    )


# AC1: working_branch unset → operate on dev branch, no seed/create/push-upstream steps
def test_no_working_branch_operates_on_dev_branch():
    cfg = Config(dev_branch="main", working_branch=None)
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    assert result.operating_branch == "main"


def test_no_working_branch_emits_no_steps():
    cfg = Config(dev_branch="main", working_branch=None)
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    assert result.steps == ()


def test_no_working_branch_does_not_checkout():
    """Startup must never checkout any branch — repo root belongs to the operator."""
    cfg = Config(dev_branch="main", working_branch=None)
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    step_types = [type(s).__name__ for s in result.steps]
    assert "Checkout" not in step_types


# AC2: working_branch set, absent both locally and on origin → fetch, seed, push (no checkout)
def test_new_working_branch_fetches_seeds_and_pushes():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    assert result.operating_branch == "feature-x"
    step_types = [type(s) for s in result.steps]
    assert Fetch in step_types
    assert Seed in step_types
    assert PushUpstream in step_types


def test_new_working_branch_does_not_checkout():
    """New working branch must be created without any checkout."""
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    step_types = [type(s).__name__ for s in result.steps]
    assert "Checkout" not in step_types


def test_new_working_branch_seeds_from_origin_dev_branch():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    seed_steps = [s for s in result.steps if isinstance(s, Seed)]
    assert len(seed_steps) == 1
    assert seed_steps[0].source == "origin/main"
    assert seed_steps[0].target == "feature-x"


def test_new_working_branch_push_upstream_targets_working_branch():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    push_steps = [s for s in result.steps if isinstance(s, PushUpstream)]
    assert len(push_steps) == 1
    assert push_steps[0].branch == "feature-x"


def test_new_working_branch_steps_in_order_fetch_seed_push():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts())
    assert isinstance(result, BranchSetupPlan)
    step_types = [type(s) for s in result.steps]
    assert step_types == [Fetch, Seed, PushUpstream]


# AC3: working_branch set and already existing → reuse as-is, no seed/reconcile steps
def test_existing_local_working_branch_reuses_without_seed():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts(working_branch_on_local=True))
    assert isinstance(result, BranchSetupPlan)
    assert result.operating_branch == "feature-x"
    step_types = [type(s) for s in result.steps]
    assert Seed not in step_types
    assert PushUpstream not in step_types


def test_existing_local_working_branch_has_no_steps():
    """Existing working branch needs no setup steps — no checkout, no seed, no push."""
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts(working_branch_on_local=True))
    assert isinstance(result, BranchSetupPlan)
    assert result.steps == ()


def test_existing_remote_working_branch_reuses_without_seed():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts(working_branch_on_origin=True))
    assert isinstance(result, BranchSetupPlan)
    assert result.operating_branch == "feature-x"
    step_types = [type(s) for s in result.steps]
    assert Seed not in step_types
    assert PushUpstream not in step_types


def test_working_branch_existing_both_locally_and_remotely_reuses_without_seed():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(
        cfg, _facts(working_branch_on_local=True, working_branch_on_origin=True)
    )
    assert isinstance(result, BranchSetupPlan)
    assert result.operating_branch == "feature-x"
    assert result.steps == ()


# AC4: dev_branch absent from origin → typed abort with operator-facing context
def test_missing_dev_branch_returns_typed_abort():
    cfg = Config(dev_branch="main", working_branch=None)
    result = resolve_branch_setup(cfg, _facts(dev_branch_on_origin=False))
    assert isinstance(result, DevBranchMissing)


def test_missing_dev_branch_abort_carries_dev_branch_name():
    cfg = Config(dev_branch="release-3", working_branch=None)
    result = resolve_branch_setup(cfg, _facts(dev_branch_on_origin=False))
    assert isinstance(result, DevBranchMissing)
    assert result.dev_branch == "release-3"


def test_missing_dev_branch_abort_has_no_setup_steps():
    cfg = Config(dev_branch="main", working_branch=None)
    result = resolve_branch_setup(cfg, _facts(dev_branch_on_origin=False))
    assert not isinstance(result, BranchSetupPlan)


def test_missing_dev_branch_with_working_branch_configured_aborts():
    cfg = Config(dev_branch="main", working_branch="feature-x")
    result = resolve_branch_setup(cfg, _facts(dev_branch_on_origin=False))
    assert isinstance(result, DevBranchMissing)
    assert result.dev_branch == "main"


# ── find_checked_out_worktrees ────────────────────────────────────────────────


def test_find_checked_out_worktrees_returns_path_when_branch_is_checked_out(
    tmp_path: Path,
) -> None:
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    result = find_checked_out_worktrees("main", [(wt1, "main"), (wt2, "feature")])
    assert result == [wt1]


def test_find_checked_out_worktrees_returns_empty_when_branch_not_checked_out(
    tmp_path: Path,
) -> None:
    wt1 = tmp_path / "wt1"
    result = find_checked_out_worktrees("main", [(wt1, "feature")])
    assert result == []


def test_find_checked_out_worktrees_excludes_detached_head(tmp_path: Path) -> None:
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    result = find_checked_out_worktrees("main", [(wt1, None), (wt2, "main")])
    assert result == [wt2]


def test_find_checked_out_worktrees_returns_multiple_paths(tmp_path: Path) -> None:
    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    result = find_checked_out_worktrees("main", [(wt1, "main"), (wt2, "main")])
    assert result == [wt1, wt2]


def test_find_checked_out_worktrees_empty_list_returns_empty(tmp_path: Path) -> None:
    result = find_checked_out_worktrees("main", [])
    assert result == []
