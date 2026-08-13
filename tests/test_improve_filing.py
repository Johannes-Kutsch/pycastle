"""Tests for file_draft_set — two-stage commit of a validated draft set."""

from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from pycastle.iteration.improve_drafts import IssueDraft
from pycastle.iteration.improve_filing import FilingPort, file_draft_set

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

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

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

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    ops = port.method_calls
    create_spec_idx = ops.index(
        call.create_issue("Spec Issue", _BODY, ["behavior-slice"])
    )
    create_foo_idx = ops.index(
        call.create_issue("01-foo Slice", _BODY, ["behavior-slice"])
    )
    create_bar_idx = ops.index(
        call.create_issue("02-bar Slice", _BODY, ["behavior-slice"])
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

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    _, body, _ = port.create_issue.call_args_list[1].args
    assert "Blocked by #100" in body


def test_blocked_slice_native_dependency_wired(tmp_path: Path, port: MagicMock) -> None:
    drafts = [_spec(), _slice("01-foo", blocked_by=["spec"])]

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    port.add_issue_dependency.assert_called_once_with(101, 1000)


def test_unblocked_slice_has_no_blocked_by_in_body(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo")]

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    _, body, _ = port.create_issue.call_args_list[1].args
    assert "Blocked by" not in body
    port.add_issue_dependency.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior 4: blocked_by handles resolve to real issue numbers
# ---------------------------------------------------------------------------


def test_slice_blocked_by_earlier_slice_resolves_to_real_number(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo"), _slice("02-bar", blocked_by=["01-foo"])]

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    _, bar_body, _ = port.create_issue.call_args_list[2].args
    assert "Blocked by #101" in bar_body
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

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    _, bar_body, _ = port.create_issue.call_args_list[2].args
    assert "#100" in bar_body
    assert "#101" in bar_body
    assert port.add_issue_dependency.call_count == 2


# ---------------------------------------------------------------------------
# Behavior 5: state label withheld during creation, applied in terminal pass
# ---------------------------------------------------------------------------


def test_state_label_absent_from_create_calls(tmp_path: Path, port: MagicMock) -> None:
    drafts = [_spec(), _slice("01-foo")]

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    for c in port.create_issue.call_args_list:
        labels = c.args[2]
        assert _STATE_LABEL not in labels


def test_state_label_applied_to_all_issues_after_creation(
    tmp_path: Path, port: MagicMock
) -> None:
    drafts = [_spec(), _slice("01-foo"), _slice("02-bar")]

    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    apply_calls = port.apply_label.call_args_list
    applied_numbers = {c.args[0] for c in apply_calls}
    applied_labels = {c.args[1] for c in apply_calls}
    assert applied_numbers == {100, 101, 102}
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
    file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

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

    with pytest.raises(RuntimeError, match="network failure"):
        file_draft_set(drafts, port=port, role_dir=tmp_path, state_label=_STATE_LABEL)

    record_file = tmp_path / "_candidate_record"
    assert record_file.is_file()

    import json

    data = json.loads(record_file.read_text(encoding="utf-8"))
    assert data["spec_number"] == 100
    assert data["labels_applied"] is False
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

    with pytest.raises(RuntimeError):
        file_draft_set(
            drafts, port=port_first, role_dir=tmp_path, state_label=_STATE_LABEL
        )

    port_second = MagicMock(spec=FilingPort)
    port_second.create_issue.side_effect = [(101, 1001)]

    file_draft_set(
        drafts, port=port_second, role_dir=tmp_path, state_label=_STATE_LABEL
    )

    # Second run must have created exactly one issue (the slice, not the spec)
    assert port_second.create_issue.call_count == 1
    title = port_second.create_issue.call_args.args[0]
    assert title == "01-foo Slice"


def test_resume_applies_labels_to_all_issues_including_previously_filed(
    tmp_path: Path,
) -> None:
    """On resume, apply_label must cover both the spec and the resumed slice."""
    port_first = MagicMock(spec=FilingPort)
    port_first.create_issue.side_effect = [
        (100, 1000),
        RuntimeError("interrupted"),
    ]

    drafts = [_spec(), _slice("01-foo")]

    with pytest.raises(RuntimeError):
        file_draft_set(
            drafts, port=port_first, role_dir=tmp_path, state_label=_STATE_LABEL
        )

    port_second = MagicMock(spec=FilingPort)
    port_second.create_issue.side_effect = [(101, 1001)]

    file_draft_set(
        drafts, port=port_second, role_dir=tmp_path, state_label=_STATE_LABEL
    )

    applied_numbers = {c.args[0] for c in port_second.apply_label.call_args_list}
    assert 100 in applied_numbers
    assert 101 in applied_numbers


# ---------------------------------------------------------------------------
# Behavior 8: candidate record lives at role_dir root
# ---------------------------------------------------------------------------


def test_candidate_record_is_at_role_dir_root(tmp_path: Path, port: MagicMock) -> None:
    """The record file lives directly under role_dir, not in any sub-directory."""
    role_dir = tmp_path / "improve"
    drafts = [_spec(), _slice("01-foo")]

    file_draft_set(drafts, port=port, role_dir=role_dir, state_label=_STATE_LABEL)

    record_file = role_dir / "_candidate_record"
    assert record_file.is_file()
    # No nested directories (beyond role_dir itself) should hold the record
    nested = [p for p in role_dir.rglob("_candidate_record") if p != record_file]
    assert not nested


def test_candidate_record_survives_nested_dir_removal(
    tmp_path: Path, port: MagicMock
) -> None:
    """The record at role_dir is not affected when a sub-directory is removed."""
    import shutil

    role_dir = tmp_path / "improve"
    namespace_dir = role_dir / "main"
    namespace_dir.mkdir(parents=True, exist_ok=True)

    drafts = [_spec(), _slice("01-foo")]
    file_draft_set(drafts, port=port, role_dir=role_dir, state_label=_STATE_LABEL)

    # Simulate namespace discard
    shutil.rmtree(namespace_dir)

    record_file = role_dir / "_candidate_record"
    assert record_file.is_file()
