from __future__ import annotations

import asyncio
import enum
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SHELL_EXPR = re.compile(r"!`([^`]+)`")


class PromptRenderError(Exception):
    pass


def _render(template: str, args: dict[str, str]) -> str:
    found = set(PLACEHOLDER.findall(template))
    missing = found - args.keys()
    if missing:
        raise PromptRenderError(f"Missing prompt args: {missing}")
    return PLACEHOLDER.sub(lambda m: args[m.group(1)], template)


async def _preprocess(prompt: str, exec_fn) -> str:
    matches = list(SHELL_EXPR.finditer(prompt))
    if not matches:
        return prompt
    results = await asyncio.gather(*[exec_fn(m.group(1)) for m in matches])
    for match, out in zip(reversed(matches), reversed(list(results))):
        prompt = prompt[: match.start()] + out.rstrip("\n") + prompt[match.end() :]
    return prompt


# ── PromptRenderer infrastructure ─────────────────────────────────────────────


class Scope(enum.Enum):
    # Values are (name, placeholders) tuples — the name disambiguates scopes
    # whose placeholder sets are equal (otherwise Enum would alias them, e.g.
    # IMPROVE_SCAN and RESUME both have empty sets).
    PER_ISSUE = (
        "PER_ISSUE",
        frozenset(
            {"ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_BODY", "ISSUE_COMMENTS", "BRANCH"}
        ),
    )
    MERGE = ("MERGE", frozenset({"BRANCHES"}))
    PLAN = ("PLAN", frozenset({"ALL_OPEN_ISSUES_JSON", "READY_FOR_AGENT_ISSUES_JSON"}))
    PREFLIGHT = ("PREFLIGHT", frozenset({"CHECK_NAME", "COMMAND", "OUTPUT"}))
    IMPROVE_SCAN = ("IMPROVE_SCAN", frozenset[str]())
    IMPROVE_SESSION = ("IMPROVE_SESSION", frozenset({"IMPROVE_SHORT_SID"}))
    IMPROVE_ISSUES = (
        "IMPROVE_ISSUES",
        frozenset(
            {
                "IMPROVE_SHORT_SID",
                "ISSUE_NUMBER",
                "ISSUE_TITLE",
                "ISSUE_BODY",
                "ISSUE_COMMENTS",
            }
        ),
    )
    RESUME = ("RESUME", frozenset[str]())
    FAILURE_REPORT = ("FAILURE_REPORT", frozenset({"FAILED_ROLE", "SESSION_DIR"}))

    @property
    def placeholders(self) -> frozenset[str]:
        return self.value[1]  # type: ignore[index,no-any-return]


class PromptTemplate(enum.Enum):
    IMPLEMENT = ("implement-prompt.md", Scope.PER_ISSUE)
    REVIEW = ("review-prompt.md", Scope.PER_ISSUE)
    MERGE = ("merge-prompt.md", Scope.MERGE)
    PLAN = ("plan-prompt.md", Scope.PLAN)
    PREFLIGHT_ISSUE = ("preflight-issue.md", Scope.PREFLIGHT)
    IMPROVE_SCAN = ("improve/01-scan.md", Scope.IMPROVE_SCAN)
    IMPROVE_PRD = ("improve/02-prd.md", Scope.IMPROVE_SESSION)
    IMPROVE_ISSUES = ("improve/03-issues.md", Scope.IMPROVE_ISSUES)
    IMPROVE_NO_CANDIDATE = ("improve/04-no-candidate-report.md", Scope.IMPROVE_SESSION)
    RESUME = ("_resume-prompt.md", Scope.RESUME)
    FAILURE_REPORT = ("failure-report.md", Scope.FAILURE_REPORT)

    @property
    def filename(self) -> str:
        return self.value[0]  # type: ignore[index]

    @property
    def scope(self) -> Scope:
        return self.value[1]  # type: ignore[index]


_ISSUE_PLACEHOLDER_KEYS = frozenset(
    {"ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_BODY", "ISSUE_COMMENTS"}
)


def _format_issue_comments(comments: Sequence[dict[str, str]]) -> str:
    parts: list[str] = []
    for c in comments:
        author = c.get("author") or "unknown"
        when = c.get("created_at") or "unknown time"
        body = c.get("body") or ""
        parts.append(f"## Comment by @{author} at {when}\n\n{body}")
    return "\n\n".join(parts)


def build_issue_scope_args(
    issue: dict, *, extra_scope_args: dict[str, str]
) -> dict[str, str]:
    collisions = _ISSUE_PLACEHOLDER_KEYS & extra_scope_args.keys()
    if collisions:
        raise PromptRenderError(
            f"extra_scope_args collides with reserved ISSUE_* keys: {collisions}"
        )
    return {
        "ISSUE_NUMBER": str(issue["number"]),
        "ISSUE_TITLE": issue["title"],
        "ISSUE_BODY": str(issue["body"] or ""),
        "ISSUE_COMMENTS": _format_issue_comments(issue["comments"]),
        **extra_scope_args,
    }


def _format_feedback_commands(checks: Sequence[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


class PromptRenderer:
    _STANDARDS_FILES: dict[str, str] = {
        "TESTING_STANDARDS": "tests.md",
        "MOCKING_STANDARDS": "mocking.md",
        "INTERFACES_STANDARDS": "interfaces.md",
        "DEEP_MODULES_STANDARDS": "deep-modules.md",
        "REFACTORING_STANDARDS": "refactoring.md",
        "LANGUAGE_STANDARDS": "language.md",
        "DEEPENING_STANDARDS": "deepening.md",
    }

    def __init__(self, cfg: Config) -> None:
        self._prompts_dir: Path = cfg.prompts_dir
        self._global_args = self._build_global_args(cfg)
        self._validate_templates()

    def _load_standards(self, prompts_dir: Path) -> dict[str, str]:
        standards_dir = prompts_dir / "coding-standards"
        result = {}
        for key, filename in self._STANDARDS_FILES.items():
            path = standards_dir / filename
            result[key] = path.read_text(encoding="utf-8") if path.exists() else ""
        return result

    def _build_global_args(self, cfg: Config) -> dict[str, str]:
        standards = self._load_standards(cfg.prompts_dir)
        checks = " && ".join(cmd for _, cmd in cfg.preflight_checks)
        return {
            "BUG_LABEL": cfg.bug_label,
            "READY_FOR_AGENT_LABEL": cfg.issue_label,
            "READY_FOR_HUMAN_LABEL": cfg.hitl_label,
            "ENHANCEMENT_LABEL": cfg.enhancement_label,
            "NEEDS_TRIAGE_LABEL": cfg.needs_triage_label,
            "NEEDS_INFO_LABEL": cfg.needs_info_label,
            "WONTFIX_LABEL": cfg.wontfix_label,
            "FEEDBACK_COMMANDS": _format_feedback_commands(cfg.implement_checks),
            "CHECKS": checks,
            **standards,
        }

    def _validate_templates(self) -> None:
        global_keys = set(self._global_args.keys())
        for template in PromptTemplate:
            path = self._prompts_dir / template.filename
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            found = set(PLACEHOLDER.findall(content))
            allowed = global_keys | template.scope.placeholders
            unknown = found - allowed
            if unknown:
                raise PromptRenderError(
                    f"Template {template.filename!r} references unknown token(s): {unknown}"
                )

    async def render(
        self,
        template: PromptTemplate,
        scope_args: dict[str, str],
        exec_fn,
    ) -> str:
        expected = template.scope.placeholders
        actual = set(scope_args.keys())
        if actual != expected:
            missing = expected - actual
            extra = actual - expected
            parts: list[str] = []
            if missing:
                parts.append(f"missing: {missing}")
            if extra:
                parts.append(f"extra: {extra}")
            raise PromptRenderError(
                f"scope_args mismatch for {template.name}: {'; '.join(parts)}"
            )

        path = self._prompts_dir / template.filename
        content = path.read_text(encoding="utf-8")
        preprocessed = await _preprocess(content, exec_fn)
        all_args = {**self._global_args, **scope_args}
        return _render(preprocessed, all_args)
