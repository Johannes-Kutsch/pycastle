import json
import re


class AgentOutputProtocolError(Exception):
    pass


class PlanParseError(AgentOutputProtocolError):
    pass


class IssueParseError(AgentOutputProtocolError):
    pass


class PromiseParseError(AgentOutputProtocolError):
    pass


def _unwrap(output: str) -> str:
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj.get("result")
            return result if isinstance(result, str) else output
    return output


def parse_plan(output: str) -> list[dict]:
    text = _unwrap(output)
    match = re.search(r"<plan>([\s\S]*?)</plan>", text)
    if not match:
        raise PlanParseError("Planner produced no <plan> tag.")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise PlanParseError(
            f"Planner produced malformed JSON inside <plan> tag: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PlanParseError(f"Plan JSON must be an object, got {type(data).__name__}.")
    if "unblocked_issues" in data:
        raw = data["unblocked_issues"]
    elif "issues" in data:
        raw = data["issues"]
    else:
        raise PlanParseError(
            f"Plan JSON has no 'unblocked_issues' or 'issues' key. Keys found: {list(data.keys())}"
        )
    try:
        return [{"number": i["number"], "title": i["title"]} for i in raw]
    except (KeyError, TypeError) as exc:
        raise PlanParseError(
            f"Plan JSON issues list has unexpected structure: {exc}"
        ) from exc


def parse_issue_number(output: str) -> tuple[str, int]:
    text = _unwrap(output)
    match = re.search(r'<issue\s+label="([^"]+)">(\S+)</issue>', text)
    if not match:
        raise IssueParseError(
            'Agent produced no <issue label="...">NUMBER</issue> tag.'
        )
    label = match.group(1)
    raw_number = match.group(2)
    try:
        number = int(raw_number)
    except ValueError as exc:
        raise IssueParseError(f"{raw_number!r} is not a valid issue number.") from exc
    return label, number


def is_complete(output: str) -> bool:
    text = _unwrap(output)
    return bool(re.search(r"<promise>COMPLETE</promise>", text))
