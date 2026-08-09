from pathlib import Path

import pytest

from pycastle.agents.output_protocol import AgentOutputProtocolError
from pycastle.config import Config
from pycastle.iteration.improve_drafts import (
    DraftSetValidationError,
    read_draft_set,
)

_VALID_BODY = "A" * 120  # well above the 100-char body floor


def _spec_draft(tmp_path: Path, *, body: str = _VALID_BODY) -> None:
    (tmp_path / "spec.md").write_text(
        f"---\ntitle: Spec Issue\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\n{body}"
    )


def _slice_draft(
    tmp_path: Path,
    name: str,
    *,
    body: str = _VALID_BODY,
    blocked_by: list[str] | None = None,
) -> None:
    fm = f"---\ntitle: {name} Slice\nlabels:\n  - behavior-slice\n  - ready-for-agent\n"
    if blocked_by:
        fm += "blocked_by:\n" + "".join(f"  - {h}\n" for h in blocked_by)
    fm += "---\n"
    (tmp_path / f"{name}.md").write_text(fm + f"\n{body}")


@pytest.fixture
def cfg() -> Config:
    return Config()


# ---------------------------------------------------------------------------
# Behavior 1: well-formed set is ordered
# ---------------------------------------------------------------------------


def test_well_formed_set_returns_spec_then_slices_in_order(
    tmp_path: Path, cfg: Config
) -> None:
    _spec_draft(tmp_path)
    _slice_draft(tmp_path, "02-bar")
    _slice_draft(tmp_path, "01-foo")

    result = read_draft_set(tmp_path, cfg)

    assert [d.handle for d in result] == ["spec", "01-foo", "02-bar"]


# ---------------------------------------------------------------------------
# Behavior 2: missing title or labels rejects the whole set
# ---------------------------------------------------------------------------


def test_missing_title_rejects_whole_set(tmp_path: Path, cfg: Config) -> None:
    (tmp_path / "spec.md").write_text(
        f"---\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\n{_VALID_BODY}"
    )
    _slice_draft(tmp_path, "01-foo")

    with pytest.raises(DraftSetValidationError) as exc_info:
        read_draft_set(tmp_path, cfg)

    assert any("title" in p for p in exc_info.value.problems)


def test_missing_labels_rejects_whole_set(tmp_path: Path, cfg: Config) -> None:
    (tmp_path / "spec.md").write_text(f"---\ntitle: Spec Issue\n---\n\n{_VALID_BODY}")
    _slice_draft(tmp_path, "01-foo")

    with pytest.raises(DraftSetValidationError) as exc_info:
        read_draft_set(tmp_path, cfg)

    assert any("labels" in p for p in exc_info.value.problems)


# ---------------------------------------------------------------------------
# Behavior 3: unknown blocked_by handle rejects the set
# ---------------------------------------------------------------------------


def test_unknown_blocked_by_handle_rejects_set(tmp_path: Path, cfg: Config) -> None:
    _spec_draft(tmp_path)
    _slice_draft(tmp_path, "01-foo", blocked_by=["nonexistent-handle"])

    with pytest.raises(DraftSetValidationError) as exc_info:
        read_draft_set(tmp_path, cfg)

    assert any("nonexistent-handle" in p for p in exc_info.value.problems)


# ---------------------------------------------------------------------------
# Behavior 4: multiple malformed drafts report all problems together
# ---------------------------------------------------------------------------


def test_multiple_malformed_drafts_reports_all_problems(
    tmp_path: Path, cfg: Config
) -> None:
    # spec missing title, slice missing labels
    (tmp_path / "spec.md").write_text(
        f"---\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\n{_VALID_BODY}"
    )
    (tmp_path / "01-foo.md").write_text(f"---\ntitle: Foo Slice\n---\n\n{_VALID_BODY}")

    with pytest.raises(DraftSetValidationError) as exc_info:
        read_draft_set(tmp_path, cfg)

    problems = exc_info.value.problems
    assert len(problems) >= 2
    assert any("title" in p and "spec" in p for p in problems)
    assert any("labels" in p and "01-foo" in p for p in problems)


# ---------------------------------------------------------------------------
# Behavior 5: body that fails readiness classification rejects the set
# ---------------------------------------------------------------------------


def test_short_body_rejects_set(tmp_path: Path, cfg: Config) -> None:
    (tmp_path / "spec.md").write_text(
        "---\ntitle: Spec\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\nToo short."
    )

    with pytest.raises(DraftSetValidationError) as exc_info:
        read_draft_set(tmp_path, cfg)

    assert any("readiness" in p and "spec" in p for p in exc_info.value.problems)


def test_missing_slice_mode_label_rejects_set(tmp_path: Path, cfg: Config) -> None:
    (tmp_path / "spec.md").write_text(
        f"---\ntitle: Spec\nlabels:\n  - ready-for-agent\n---\n\n{_VALID_BODY}"
    )

    with pytest.raises(DraftSetValidationError) as exc_info:
        read_draft_set(tmp_path, cfg)

    assert any("readiness" in p and "spec" in p for p in exc_info.value.problems)


# ---------------------------------------------------------------------------
# Behavior 6: rejection is raised as AgentOutputProtocolError
# ---------------------------------------------------------------------------


def test_validation_error_is_protocol_error(tmp_path: Path, cfg: Config) -> None:
    (tmp_path / "spec.md").write_text(
        f"---\nlabels:\n  - behavior-slice\n  - ready-for-agent\n---\n\n{_VALID_BODY}"
    )

    with pytest.raises(AgentOutputProtocolError):
        read_draft_set(tmp_path, cfg)


def test_draft_set_validation_error_is_subclass() -> None:
    assert issubclass(DraftSetValidationError, AgentOutputProtocolError)
