"""Tests for sectioned_markdown_body — parse/edit/render of ## -sectioned bodies."""

from pycastle.iteration.sectioned_markdown_body import (
    Anchor,
    AnchorKind,
    OnMissing,
    SectionedMarkdownBody,
)

# ---------------------------------------------------------------------------
# Behavior 1: module exists with correct public interface
# ---------------------------------------------------------------------------


def test_module_exposes_public_interface():
    body = SectionedMarkdownBody("## Foo\n\nsome text")
    result = body.render()
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Behavior 2: upsert-before places section immediately before exact anchor
# ---------------------------------------------------------------------------


def test_upsert_before_exact_anchor_present():
    body = SectionedMarkdownBody(
        "## Acceptance criteria\n\n- item\n\n## Files touched\n\n- file.py"
    )
    result = body.upsert_before(
        "## Blocked by", "None", ["## Acceptance criteria"]
    ).render()
    sections = [line for line in result.split("\n") if line.startswith("## ")]
    assert sections == ["## Blocked by", "## Acceptance criteria", "## Files touched"]


# ---------------------------------------------------------------------------
# Behavior 3: upsert-before ordered anchor list — first absent, second present
# ---------------------------------------------------------------------------


def test_upsert_before_ordered_anchors_uses_second_when_first_absent():
    body = SectionedMarkdownBody(
        "## What to build\n\ndetail\n\n## Files touched\n\nfile.py"
    )
    result = body.upsert_before(
        "## Parent",
        "#42",
        ["## Acceptance criteria", "## What to build"],
    ).render()
    sections = [line for line in result.split("\n") if line.startswith("## ")]
    assert sections == ["## Parent", "## What to build", "## Files touched"]


# ---------------------------------------------------------------------------
# Behavior 4: upsert-before with no matching anchor and on_missing=PREPEND
# ---------------------------------------------------------------------------


def test_upsert_before_no_anchor_prepend_goes_to_top():
    body = SectionedMarkdownBody(
        "## What to build\n\ndetail\n\n## Files touched\n\nfile.py"
    )
    result = body.upsert_before(
        "## Parent",
        "#1",
        ["## No such section"],
        on_missing=OnMissing.PREPEND,
    ).render()
    sections = [line for line in result.split("\n") if line.startswith("## ")]
    assert sections[0] == "## Parent"
    assert sections == ["## Parent", "## What to build", "## Files touched"]


# ---------------------------------------------------------------------------
# Behavior 5: upsert-after with exact-heading anchor present
# ---------------------------------------------------------------------------


def test_upsert_after_exact_anchor_present():
    body = SectionedMarkdownBody(
        "## Acceptance criteria\n\n- item\n\n## Files touched\n\nfile.py"
    )
    result = body.upsert_after(
        "## Blocked by", "None", ["## Acceptance criteria"]
    ).render()
    sections = [line for line in result.split("\n") if line.startswith("## ")]
    assert sections == ["## Acceptance criteria", "## Blocked by", "## Files touched"]


# ---------------------------------------------------------------------------
# Behavior 6: upsert-after with no matching anchor and on_missing=APPEND
# ---------------------------------------------------------------------------


def test_upsert_after_no_anchor_append_goes_to_end():
    body = SectionedMarkdownBody(
        "## What to build\n\ndetail\n\n## Acceptance criteria\n\n- x"
    )
    result = body.upsert_after(
        "## Blocked by",
        "None",
        ["## No such section"],
        on_missing=OnMissing.APPEND,
    ).render()
    sections = [line for line in result.split("\n") if line.startswith("## ")]
    assert sections[-1] == "## Blocked by"


# ---------------------------------------------------------------------------
# Behavior 7: upsert on body already carrying same heading → exactly one copy
# ---------------------------------------------------------------------------


def test_upsert_deduplicates_existing_section():
    body = SectionedMarkdownBody(
        "## Parent\n\n#99\n\n## What to build\n\ndetail\n\n## Blocked by\n\n#5"
    )
    result = body.upsert_before(
        "## Parent",
        "#1",
        ["## What to build"],
    ).render()
    heading_occurrences = [line for line in result.split("\n") if line == "## Parent"]
    assert len(heading_occurrences) == 1


# ---------------------------------------------------------------------------
# Behavior 8: remove-by-heading on absent heading → byte-identical output
# ---------------------------------------------------------------------------


def test_remove_absent_heading_returns_identical_body():
    original = "## What to build\n\nsome detail\n\n## Acceptance criteria\n\n- item"
    result = SectionedMarkdownBody(original).remove("## No such section").render()
    assert result == original


# ---------------------------------------------------------------------------
# Behavior 9: body with leading prose round-trips intact
# ---------------------------------------------------------------------------


def test_leading_prose_round_trips_without_spurious_blank_line():
    body_text = "Some leading prose.\n\n## Section One\n\ncontent"
    result = SectionedMarkdownBody(body_text).render()
    assert result.startswith("Some leading prose.")
    assert not result.startswith("\n")
    assert "## Section One" in result


# ---------------------------------------------------------------------------
# Behavior 10: heading with empty content renders without trailing blank line
# ---------------------------------------------------------------------------


def test_heading_with_empty_content_renders_alone():
    body_text = "## Empty heading\n\n## Next section\n\ncontent"
    result = SectionedMarkdownBody(body_text).render()
    lines = result.split("\n")
    empty_idx = lines.index("## Empty heading")
    assert lines[empty_idx + 1] == ""
    assert lines[empty_idx + 2] == "## Next section"


# ---------------------------------------------------------------------------
# Behavior 11: sections separated by exactly one blank line
# ---------------------------------------------------------------------------


def test_sections_separated_by_exactly_one_blank_line():
    body = SectionedMarkdownBody("## A\n\ntext A\n\n## B\n\ntext B\n\n## C\n\ntext C")
    result = body.render()
    assert "\n\n\n" not in result
    # Each pair of adjacent sections has exactly one blank line between them
    parts = result.split("\n\n")
    assert len(parts) >= 3


# ---------------------------------------------------------------------------
# Behavior 12: substring-anchor lookup selects heading containing substring
# ---------------------------------------------------------------------------


def test_upsert_before_substring_anchor_matches_by_substring():
    body = SectionedMarkdownBody(
        "## Acceptance criteria\n\n- x\n\n## Files touched (tentative)\n\nfile.py"
    )
    result = body.upsert_before(
        "## Blocked by",
        "None",
        [Anchor("Files touched", AnchorKind.SUBSTRING)],
    ).render()
    sections = [line for line in result.split("\n") if line.startswith("## ")]
    blocked_idx = sections.index("## Blocked by")
    files_idx = sections.index("## Files touched (tentative)")
    assert blocked_idx == files_idx - 1


# ---------------------------------------------------------------------------
# Behavior 13: empty input body round-trips as empty string
# ---------------------------------------------------------------------------


def test_empty_body_round_trips_as_empty_string():
    assert SectionedMarkdownBody("").render() == ""


# ---------------------------------------------------------------------------
# Behavior 14: on_missing=RAISE raises when no anchor matches
# ---------------------------------------------------------------------------


def test_upsert_before_raise_on_missing_raises():
    import pytest

    body = SectionedMarkdownBody("## Acceptance criteria\n\n- item")
    with pytest.raises(ValueError, match="on_missing=RAISE"):
        body.upsert_before(
            "## Parent",
            "#1",
            ["## No such section"],
            on_missing=OnMissing.RAISE,
        )


def test_upsert_after_raise_on_missing_raises():
    import pytest

    body = SectionedMarkdownBody("## Acceptance criteria\n\n- item")
    with pytest.raises(ValueError, match="on_missing=RAISE"):
        body.upsert_after(
            "## Blocked by",
            "None",
            ["## No such section"],
            on_missing=OnMissing.RAISE,
        )
