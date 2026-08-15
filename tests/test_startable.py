from pycastle.iteration.startable import startable_issues


def _issue(number: int, blocked_by: int = 0) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "issue_dependencies_summary": {"blocked_by": blocked_by},
    }


# AC1: ready-for-agent issues with zero open blockers are returned in input order
def test_unblocked_issues_are_returned():
    issues = [_issue(1), _issue(2), _issue(3)]
    result = startable_issues(issues, in_flight=set())
    assert [i["number"] for i in result] == [1, 2, 3]


def test_blocked_issues_are_excluded():
    issues = [_issue(1), _issue(2, blocked_by=1), _issue(3)]
    result = startable_issues(issues, in_flight=set())
    assert [i["number"] for i in result] == [1, 3]


def test_input_order_is_preserved():
    issues = [_issue(10), _issue(5), _issue(20)]
    result = startable_issues(issues, in_flight=set())
    assert [i["number"] for i in result] == [10, 5, 20]


# AC2: an in-flight issue is returned regardless of its open-blocker count
def test_in_flight_issue_with_blockers_is_returned():
    issues = [_issue(1, blocked_by=3)]
    result = startable_issues(issues, in_flight={1})
    assert [i["number"] for i in result] == [1]


def test_in_flight_issue_without_blockers_is_returned():
    issues = [_issue(1), _issue(2, blocked_by=2)]
    result = startable_issues(issues, in_flight={2})
    assert [i["number"] for i in result] == [1, 2]


def test_blocked_non_in_flight_issue_is_excluded():
    issues = [_issue(1, blocked_by=1), _issue(2)]
    result = startable_issues(issues, in_flight={2})
    assert [i["number"] for i in result] == [2]


# AC3: empty input returns empty result
def test_empty_issues_returns_empty():
    result = startable_issues([], in_flight=set())
    assert result == []


def test_empty_issues_with_in_flight_returns_empty():
    result = startable_issues([], in_flight={1, 2})
    assert result == []


def test_all_blocked_returns_empty():
    issues = [_issue(1, blocked_by=2), _issue(2, blocked_by=1)]
    result = startable_issues(issues, in_flight=set())
    assert result == []


def test_missing_issue_dependencies_summary_treats_issue_as_startable():
    issue = {"number": 1, "title": "Issue 1"}
    result = startable_issues([issue], in_flight=set())
    assert [i["number"] for i in result] == [1]


def test_missing_blocked_by_field_treats_issue_as_startable():
    issue = {"number": 1, "title": "Issue 1", "issue_dependencies_summary": {}}
    result = startable_issues([issue], in_flight=set())
    assert [i["number"] for i in result] == [1]
