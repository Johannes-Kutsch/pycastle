"""Tests for file_draft_set — two-stage commit of a validated draft set."""

import json
import shutil
from pathlib import Path
from unittest.mock import ANY, MagicMock, call

import pytest

from pycastle.iteration.improve_drafts import IssueDraft
from pycastle.iteration.improve_filing import FilingPort, file_draft_set
from pycastle.iteration.improve_role_session_store import ImproveRoleSessionStore

_BODY = "A" * 200

_STATE_LABEL = "ready-for-agent"


def _spec(body: str = _BODY) -> IssueDraft:
    return IssueDraft(
        handle="spec",
        title="Spec Issue",
        labels=["behavior-slice", _STATE_LABEL],
        body=body,
    )


def _slice(
    name: str,
    *,
    blocked_by: list[str] | None = None,
    body: str = _BODY,
) -> IssueDraft:
    return IssueDraft(
        handle=name,
        title=f"{name} Slice",
        labels=["behavior-slice", _STATE_LABEL],
        body=body,
        blocked_by=blocked_by or [],
    )


@pytest.fixture
def port() -> MagicMock:
    p = MagicMock(spec=FilingPort)
    p.create_issue.side_effect = [
        (100, 1000),
        (101, 1001),
        (102, 1002),
        (103, 1003),
    ]
    return p


# ---------------------------------------------------------------------------
# Behavior 1: spec created first, then slices in filename order
# ---------------------------------------------------------------------------


def test_spec_created_first_then_slices_in_order(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo"), _slice("02-bar")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    titles = [c.args[0] for c in port.create_issue.call_args_list]
    assert titles == ["Spec Issue", "01-foo Slice", "02-bar Slice"]


# ---------------------------------------------------------------------------
# Behavior 2: each slice registered as sub-issue and blocking edges wired
#             before the next slice is created
# ---------------------------------------------------------------------------


def test_sub_issue_and_blockers_wired_before_next_slice(
    tmp_path: Path, port: MagicMock
) -> None:
    """register_sub_issue and add_issue_dependency for slice N happen
    before create_issue for slice N+1."""
    drafts = [_spec(), _slice("01-foo"), _slice("02-bar")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    ops = port.method_calls
    create_spec_idx = ops.index(
        call.create_issue("Spec Issue", _BODY, ["behavior-slice"])
    )
    create_foo_idx = ops.index(
        call.create_issue("01-foo Slice", ANY, ["behavior-slice"])
    )
    create_bar_idx = ops.index(
        call.create_issue("02-bar Slice", ANY, ["behavior-slice"])
    )
    register_foo_idx = ops.index(call.register_sub_issue(100, 1001))
    register_bar_idx = ops.index(call.register_sub_issue(100, 1002))

    assert create_spec_idx < create_foo_idx < create_bar_idx
    assert create_foo_idx < register_foo_idx < create_bar_idx
    assert create_bar_idx < register_bar_idx


# ---------------------------------------------------------------------------
# Behavior 3: slice body contains "Blocked by" line, native deps wired
# ---------------------------------------------------------------------------


def test_blocked_slice_body_contains_blocked_by_line(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo", blocked_by=["spec"])]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    assert "## Blocked by" in body
    assert "#100" in body


def test_blocked_slice_native_dependency_wired(tmp_path: Path, port: MagicMock) -> None:
    drafts = [_spec(), _slice("01-foo", blocked_by=["spec"])]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    port.add_issue_dependency.assert_called_once_with(101, 1000)


def test_unblocked_slice_has_no_blocked_by_in_body(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    assert "## Blocked by" in body
    assert "None" in body
    port.add_issue_dependency.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior 4: blocked_by handles resolve to real issue numbers
# ---------------------------------------------------------------------------


def test_slice_blocked_by_earlier_slice_resolves_to_real_number(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo"), _slice("02-bar", blocked_by=["01-foo"])]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, bar_body, _ = port.create_issue.call_args_list[2].args
    assert "## Blocked by" in bar_body
    assert "#101" in bar_body
    # native dep: child=102, blocker_db_id=1001
    port.add_issue_dependency.assert_called_once_with(102, 1001)


def test_slice_blocked_by_spec_and_earlier_slice(
    tmp_path: Path, port: MagicMock
) -> None:
    port.create_issue.side_effect = [(100, 1000), (101, 1001), (102, 1002)]
    drafts = [
        _spec(),
        _slice("01-foo"),
        _slice("02-bar", blocked_by=["spec", "01-foo"]),
    ]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, bar_body, _ = port.create_issue.call_args_list[2].args
    assert "#100" in bar_body
    assert "#101" in bar_body
    assert port.add_issue_dependency.call_count == 2


# ---------------------------------------------------------------------------
# Behavior 5: state label withheld during creation, applied in terminal pass
#             to slices only — spec carries no state label
# ---------------------------------------------------------------------------


def test_state_label_absent_from_create_calls(tmp_path: Path, port: MagicMock) -> None:
    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    for c in port.create_issue.call_args_list:
        labels = c.args[2]
        assert _STATE_LABEL not in labels


def test_state_label_applied_to_slices_only_not_spec(
    tmp_path: Path, port: MagicMock
) -> None:
    """After filing, every slice carries the state label and the spec carries none."""
    drafts = [_spec(), _slice("01-foo"), _slice("02-bar")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    applied_numbers = {c.args[0] for c in port.apply_label.call_args_list}
    # spec is issue 100; slices are 101 and 102
    assert 100 not in applied_numbers  # spec must never receive state label
    assert applied_numbers == {101, 102}


def test_state_label_applied_to_all_slices_after_creation(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo"), _slice("02-bar")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    apply_calls = port.apply_label.call_args_list
    applied_numbers = {c.args[0] for c in apply_calls}
    applied_labels = {c.args[1] for c in apply_calls}
    assert applied_numbers == {101, 102}  # spec (100) excluded
    assert applied_labels == {_STATE_LABEL}


def test_state_label_not_applied_until_all_issues_exist(
    tmp_path: Path, port: MagicMock
) -> None:
    """apply_label must not be called until after the last create_issue."""
    ops: list[tuple[str, object]] = []
    returns = iter([(100, 1000), (101, 1001), (102, 1002)])

    def _create(title: str, body: str, labels: list[str]) -> tuple[int, int]:
        ops.append(("create", title))
        return next(returns)

    def _apply(number: int, label: str) -> None:
        ops.append(("apply", number))

    port.create_issue.side_effect = _create
    port.apply_label.side_effect = _apply

    drafts = [_spec(), _slice("01-foo"), _slice("02-bar")]
    store = ImproveRoleSessionStore(tmp_path)
    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    create_positions = [i for i, op in enumerate(ops) if op[0] == "create"]
    apply_positions = [i for i, op in enumerate(ops) if op[0] == "apply"]
    assert max(create_positions) < min(apply_positions)


# ---------------------------------------------------------------------------
# Behavior 6: failure partway through: record has spec, no state label applied
# ---------------------------------------------------------------------------


def test_failure_after_spec_leaves_record_with_spec_and_no_label(
    tmp_path: Path,
) -> None:
    port = MagicMock(spec=FilingPort)
    port.create_issue.side_effect = [
        (100, 1000),
        RuntimeError("network failure"),
    ]

    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    with pytest.raises(RuntimeError, match="network failure"):
        file_draft_set(
            drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
        )

    record = store.read_candidate_record(0)
    assert record is not None
    assert record.spec_number == 100
    assert record.labels_applied is False
    port.apply_label.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior 7: resume — record names created spec → no second spec created
# ---------------------------------------------------------------------------


def test_resume_does_not_create_second_spec(tmp_path: Path) -> None:
    """First run: spec created, then fails. Second run: spec not re-created."""
    port_first = MagicMock(spec=FilingPort)
    port_first.create_issue.side_effect = [
        (100, 1000),
        RuntimeError("interrupted"),
    ]

    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    with pytest.raises(RuntimeError):
        file_draft_set(
            drafts,
            port=port_first,
            store=store,
            candidate_idx=0,
            state_label=_STATE_LABEL,
        )

    port_second = MagicMock(spec=FilingPort)
    port_second.create_issue.side_effect = [(101, 1001)]

    file_draft_set(
        drafts, port=port_second, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    # Second run must have created exactly one issue (the slice, not the spec)
    assert port_second.create_issue.call_count == 1
    title = port_second.create_issue.call_args.args[0]
    assert title == "01-foo Slice"


def test_resume_applies_state_label_to_slices_only_not_spec(
    tmp_path: Path,
) -> None:
    """Resuming a candidate applies the state label to remaining slices; spec stays unlabelled."""
    port_first = MagicMock(spec=FilingPort)
    port_first.create_issue.side_effect = [
        (100, 1000),
        RuntimeError("interrupted"),
    ]

    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    with pytest.raises(RuntimeError):
        file_draft_set(
            drafts,
            port=port_first,
            store=store,
            candidate_idx=0,
            state_label=_STATE_LABEL,
        )

    port_second = MagicMock(spec=FilingPort)
    port_second.create_issue.side_effect = [(101, 1001)]

    file_draft_set(
        drafts, port=port_second, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    applied_numbers = {c.args[0] for c in port_second.apply_label.call_args_list}
    assert 100 not in applied_numbers  # spec must not receive state label on resume
    assert 101 in applied_numbers


def test_resume_applies_labels_to_all_slices_including_previously_filed(
    tmp_path: Path,
) -> None:
    """On resume, apply_label must cover the resumed slice but not the spec."""
    port_first = MagicMock(spec=FilingPort)
    port_first.create_issue.side_effect = [
        (100, 1000),
        RuntimeError("interrupted"),
    ]

    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    with pytest.raises(RuntimeError):
        file_draft_set(
            drafts,
            port=port_first,
            store=store,
            candidate_idx=0,
            state_label=_STATE_LABEL,
        )

    port_second = MagicMock(spec=FilingPort)
    port_second.create_issue.side_effect = [(101, 1001)]

    file_draft_set(
        drafts, port=port_second, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    applied_numbers = {c.args[0] for c in port_second.apply_label.call_args_list}
    assert 100 not in applied_numbers  # spec must not receive state label
    assert 101 in applied_numbers


# ---------------------------------------------------------------------------
# Behavior 8: candidate record is durably persisted and readable via the store
# ---------------------------------------------------------------------------


def test_candidate_record_is_durably_persisted_via_store(
    tmp_path: Path, port: MagicMock
) -> None:
    """The record is durably written and readable via the store after filing."""
    store = ImproveRoleSessionStore(tmp_path / "improve")
    drafts = [_spec(), _slice("01-foo")]

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    record = store.read_candidate_record(0)
    assert record is not None
    assert record.spec_number == 100


def test_candidate_record_survives_nested_dir_removal(
    tmp_path: Path, port: MagicMock
) -> None:
    """The record is not affected when an unrelated sub-directory is removed."""
    role_dir = tmp_path / "improve"
    namespace_dir = role_dir / "main"
    namespace_dir.mkdir(parents=True, exist_ok=True)

    store = ImproveRoleSessionStore(role_dir)
    drafts = [_spec(), _slice("01-foo")]
    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    # Simulate namespace discard
    shutil.rmtree(namespace_dir)

    record = store.read_candidate_record(0)
    assert record is not None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_drafts_is_no_op(tmp_path: Path) -> None:
    port = MagicMock(spec=FilingPort)
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        [], port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    port.create_issue.assert_not_called()
    port.apply_label.assert_not_called()


def test_spec_only_draft_does_not_get_state_label_applied(tmp_path: Path) -> None:
    port = MagicMock(spec=FilingPort)
    port.create_issue.side_effect = [(100, 1000)]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        [_spec()], port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    port.create_issue.assert_called_once()
    assert _STATE_LABEL not in port.create_issue.call_args.args[2]
    port.apply_label.assert_not_called()  # spec carries no state label
    port.register_sub_issue.assert_not_called()


def test_full_resume_with_labels_pending_only_applies_labels(tmp_path: Path) -> None:
    """When all issues are already filed but labels not yet applied, only apply_label runs."""
    port_first = MagicMock(spec=FilingPort)
    port_first.create_issue.side_effect = [(100, 1000), (101, 1001)]
    port_first.apply_label.side_effect = RuntimeError("network failure during label")

    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    with pytest.raises(RuntimeError, match="network failure during label"):
        file_draft_set(
            drafts,
            port=port_first,
            store=store,
            candidate_idx=0,
            state_label=_STATE_LABEL,
        )

    # Record has both issues filed; labels_applied still False
    record = store.read_candidate_record(0)
    assert record is not None
    assert record.spec_number == 100
    assert len(record.filed_slices) == 1
    assert record.labels_applied is False

    port_second = MagicMock(spec=FilingPort)

    file_draft_set(
        drafts, port=port_second, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    port_second.create_issue.assert_not_called()
    applied_numbers = {c.args[0] for c in port_second.apply_label.call_args_list}
    assert applied_numbers == {101}  # spec (100) excluded


# ---------------------------------------------------------------------------
# Behavior 9: cross-candidate blocking — slices of candidate N+1 are blocked
# by candidate N's spec issue (body + native dep)
# ---------------------------------------------------------------------------


def _make_port(*return_values: tuple[int, int]) -> MagicMock:
    p = MagicMock(spec=FilingPort)
    p.create_issue.side_effect = list(return_values)
    return p


def test_second_candidate_slices_blocked_by_prev_spec_in_body(
    tmp_path: Path,
) -> None:
    """Slices of the second candidate have 'Blocked by #prev_spec' in their body."""
    port = _make_port((200, 2000), (201, 2001))
    drafts = [_spec(), _slice("01-alpha")]
    store = ImproveRoleSessionStore(tmp_path / "c2")

    file_draft_set(
        drafts,
        port=port,
        store=store,
        candidate_idx=0,
        state_label=_STATE_LABEL,
        prev_spec=(100, 1000),
    )

    _, slice_body, _ = port.create_issue.call_args_list[1].args
    assert "## Blocked by" in slice_body
    assert "#100" in slice_body


def test_second_candidate_slices_have_native_dep_on_prev_spec(
    tmp_path: Path,
) -> None:
    """Slices of the second candidate wire add_issue_dependency to prev spec db_id."""
    port = _make_port((200, 2000), (201, 2001))
    drafts = [_spec(), _slice("01-alpha")]
    store = ImproveRoleSessionStore(tmp_path / "c2")

    file_draft_set(
        drafts,
        port=port,
        store=store,
        candidate_idx=0,
        state_label=_STATE_LABEL,
        prev_spec=(100, 1000),
    )

    port.add_issue_dependency.assert_called_once_with(201, 1000)


def test_first_candidate_slices_carry_no_cross_candidate_blocker(
    tmp_path: Path,
) -> None:
    """When prev_spec is None (first candidate), slices have no cross-candidate blocker."""
    port = _make_port((100, 1000), (101, 1001))
    drafts = [_spec(), _slice("01-alpha")]
    store = ImproveRoleSessionStore(tmp_path / "c1")

    file_draft_set(
        drafts,
        port=port,
        store=store,
        candidate_idx=0,
        state_label=_STATE_LABEL,
    )

    _, slice_body, _ = port.create_issue.call_args_list[1].args
    assert "## Blocked by" in slice_body
    assert "None" in slice_body
    port.add_issue_dependency.assert_not_called()


def test_second_candidate_spec_not_blocked_by_prev_spec(
    tmp_path: Path,
) -> None:
    """The spec issue of a later candidate is NOT blocked by the previous spec."""
    port = _make_port((200, 2000), (201, 2001))
    drafts = [_spec(), _slice("01-alpha")]
    store = ImproveRoleSessionStore(tmp_path / "c2")

    file_draft_set(
        drafts,
        port=port,
        store=store,
        candidate_idx=0,
        state_label=_STATE_LABEL,
        prev_spec=(100, 1000),
    )

    _, spec_body, _ = port.create_issue.call_args_list[0].args
    assert "Blocked by #100" not in spec_body


def test_cross_candidate_blocker_alongside_intra_set_blockers(
    tmp_path: Path,
) -> None:
    """Cross-candidate blocker appears in same 'Blocked by' line as intra-set blockers."""
    port = _make_port((200, 2000), (201, 2001), (202, 2002))
    drafts = [_spec(), _slice("01-alpha"), _slice("02-beta", blocked_by=["01-alpha"])]
    store = ImproveRoleSessionStore(tmp_path / "c2")

    file_draft_set(
        drafts,
        port=port,
        store=store,
        candidate_idx=0,
        state_label=_STATE_LABEL,
        prev_spec=(100, 1000),
    )

    _, beta_body, _ = port.create_issue.call_args_list[2].args
    # Both blockers must appear
    assert "#201" in beta_body  # intra-set: 01-alpha
    assert "#100" in beta_body  # cross-candidate: prev spec
    # Only one "Blocked by" line
    assert beta_body.count("Blocked by") == 1


def test_single_candidate_unchanged_behavior(tmp_path: Path) -> None:
    """Filing one candidate without prev_spec produces the same output as before."""
    port = _make_port((100, 1000), (101, 1001))
    drafts = [_spec(), _slice("01-alpha")]
    store = ImproveRoleSessionStore(tmp_path / "c1")

    file_draft_set(
        drafts,
        port=port,
        store=store,
        candidate_idx=0,
        state_label=_STATE_LABEL,
    )

    assert port.create_issue.call_count == 2
    _, slice_body, _ = port.create_issue.call_args_list[1].args
    assert "## Blocked by" in slice_body
    assert "None" in slice_body
    port.add_issue_dependency.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior 10: backward compatibility — old records with prd_number load cleanly
# ---------------------------------------------------------------------------


def test_old_record_with_prd_number_still_loads(tmp_path: Path) -> None:
    """A candidate record written before prd_number was removed loads without error.

    The extra key is silently ignored; the spec is recognized as already filed
    so file_draft_set does not create a duplicate.
    """
    old_record = {
        "spec_number": 100,
        "spec_database_id": 1000,
        "spec_title": "Spec Issue",
        "filed_slices": [],
        "labels_applied": False,
        "prd_number": 42,
    }
    store = ImproveRoleSessionStore(tmp_path)
    # Write old-format JSON directly to the path that the store reads from
    candidate_dir = tmp_path / "candidates" / "0"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "_candidate_record").write_text(
        json.dumps(old_record), encoding="utf-8"
    )

    port = MagicMock(spec=FilingPort)
    port.create_issue.side_effect = [(101, 1001)]

    drafts = [_spec(), _slice("01-foo")]
    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    # spec must not be re-created — it was already present in the old record
    titles = [c.args[0] for c in port.create_issue.call_args_list]
    assert "Spec Issue" not in titles


# ---------------------------------------------------------------------------
# Behavior 11: ## Parent section in every filed slice body
# ---------------------------------------------------------------------------

# Canonical body matching the sub-issue template structure from the improve prompt.
_CANONICAL_SLICE_BODY = (
    "## What to build\n\nDo the thing.\n\n"
    "## Acceptance criteria\n\n- [ ] It works.\n\n"
    "## Blocked by\n\nPlaceholder.\n\n"
    "## Files touched (tentative)\n\n- src/some/module.py"
)


def test_filed_slice_body_has_parent_section_naming_spec(
    tmp_path: Path, port: MagicMock
) -> None:
    """Every filed slice body contains a ## Parent section with the spec issue number."""
    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    assert "## Parent" in body
    assert "#100" in body


# ---------------------------------------------------------------------------
# Behavior 12: ## Blocked by section always present (with refs or "None")
# ---------------------------------------------------------------------------


def test_blocked_slice_body_has_blocked_by_section_with_issue_refs(
    tmp_path: Path, port: MagicMock
) -> None:
    """A slice with blockers has a ## Blocked by section containing the issue refs."""
    drafts = [_spec(), _slice("01-foo", blocked_by=["spec"])]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    assert "## Blocked by" in body
    assert "#100" in body


def test_unblocked_slice_body_has_blocked_by_section_stating_none(
    tmp_path: Path, port: MagicMock
) -> None:
    """An unblocked slice still has a ## Blocked by section stating there are none."""
    drafts = [_spec(), _slice("01-foo")]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    assert "## Blocked by" in body
    assert "None" in body


# ---------------------------------------------------------------------------
# Behavior 13: canonical positions — not appended after the last section
# ---------------------------------------------------------------------------


def test_parent_section_precedes_what_to_build_in_canonical_body(
    tmp_path: Path,
) -> None:
    """## Parent appears before ## What to build when the body has canonical structure."""
    port = MagicMock(spec=FilingPort)
    port.create_issue.side_effect = [(100, 1000), (101, 1001)]
    store = ImproveRoleSessionStore(tmp_path)

    drafts = [
        _spec(body=_CANONICAL_SLICE_BODY),
        IssueDraft(
            handle="01-foo",
            title="01-foo Slice",
            labels=["behavior-slice", _STATE_LABEL],
            body=_CANONICAL_SLICE_BODY,
        ),
    ]

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    parent_pos = body.index("## Parent")
    what_pos = body.index("## What to build")
    assert parent_pos < what_pos


def test_blocked_by_section_precedes_files_touched_in_canonical_body(
    tmp_path: Path,
) -> None:
    """Resolved refs appear within ## Blocked by, before ## Files touched.

    The old appended approach leaves refs after ## Files touched; the canonical
    approach places them inside the ## Blocked by section.
    """
    port = MagicMock(spec=FilingPort)
    port.create_issue.side_effect = [(100, 1000), (101, 1001)]
    store = ImproveRoleSessionStore(tmp_path)

    drafts = [
        _spec(body=_CANONICAL_SLICE_BODY),
        IssueDraft(
            handle="01-foo",
            title="01-foo Slice",
            labels=["behavior-slice", _STATE_LABEL],
            body=_CANONICAL_SLICE_BODY,
            blocked_by=["spec"],
        ),
    ]

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    blocked_pos = body.index("## Blocked by")
    files_pos = body.index("## Files touched")
    assert blocked_pos < files_pos
    # Ref must be inside the ## Blocked by section, not appended after ## Files touched
    assert "#100" in body[blocked_pos:files_pos]


# ---------------------------------------------------------------------------
# Behavior 14: draft body already contains ## Parent — host wins
# ---------------------------------------------------------------------------


def test_draft_body_with_existing_parent_section_is_overwritten(
    tmp_path: Path, port: MagicMock
) -> None:
    """If a draft body already carries ## Parent, the host replaces it with the correct ref."""
    stale_body = (
        "## Parent\n\n#999\n\n"
        "## What to build\n\nDo the thing.\n\n"
        "## Acceptance criteria\n\n- [ ] It works."
    )
    drafts = [_spec(), _slice("01-foo", body=stale_body)]
    store = ImproveRoleSessionStore(tmp_path)

    file_draft_set(
        drafts, port=port, store=store, candidate_idx=0, state_label=_STATE_LABEL
    )

    _, body, _ = port.create_issue.call_args_list[1].args
    assert body.count("## Parent") == 1
    assert "#100" in body
    assert "#999" not in body
