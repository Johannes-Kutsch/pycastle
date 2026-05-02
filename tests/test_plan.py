from pycastle.iteration.preflight import strip_stale_blocker_refs


# ── strip_stale_blocker_refs ──────────────────────────────────────────────────


def test_strip_stale_blocker_refs_removes_line_referencing_closed_blocker():
    issues = [{"number": 1, "title": "A", "body": "Blocked by #99\nOther content"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == "Other content"


def test_strip_stale_blocker_refs_handles_none_body():
    issues = [{"number": 1, "title": "A", "body": None}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_preserves_line_referencing_open_blocker():
    issues = [
        {"number": 1, "title": "A", "body": "Blocked by #2\nContent"},
        {"number": 2, "title": "B", "body": ""},
    ]
    result = strip_stale_blocker_refs(issues)
    assert "Blocked by #2" in result[0]["body"]


def test_strip_stale_blocker_refs_empty_list():
    assert strip_stale_blocker_refs([]) == []


def test_strip_stale_blocker_refs_handles_missing_body_key():
    issues = [{"number": 1, "title": "A"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == ""


def test_strip_stale_blocker_refs_preserves_other_fields():
    issues = [{"number": 7, "title": "T", "state": "open", "body": "Blocked by #99"}]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["number"] == 7
    assert result[0]["title"] == "T"
    assert result[0]["state"] == "open"


def test_strip_stale_blocker_refs_keeps_line_when_one_of_two_blockers_is_open():
    issues = [
        {"number": 1, "title": "A", "body": "Blocked by #2 and #99"},
        {"number": 2, "title": "B", "body": ""},
    ]
    result = strip_stale_blocker_refs(issues)
    assert "Blocked by #2 and #99" in result[0]["body"]


def test_strip_stale_blocker_refs_removes_line_when_all_blockers_are_closed():
    issues = [
        {"number": 1, "title": "A", "body": "Blocked by #98 and #99\nOther content"},
    ]
    result = strip_stale_blocker_refs(issues)
    assert result[0]["body"] == "Other content"
