from __future__ import annotations

import asyncio
import enum
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SHELL_EXPR = re.compile(r"!`([^`]+)`")

_STANDARDS_FILES = {
    "TESTING_STANDARDS": "tests.md",
    "MOCKING_STANDARDS": "mocking.md",
    "INTERFACES_STANDARDS": "interfaces.md",
    "DEEP_MODULES_STANDARDS": "deep-modules.md",
    "REFACTORING_STANDARDS": "refactoring.md",
}


def load_standards(prompts_dir: Path) -> dict[str, str]:
    standards_dir = prompts_dir / "coding-standards"
    result = {}
    for key, filename in _STANDARDS_FILES.items():
        path = standards_dir / filename
        result[key] = path.read_text(encoding="utf-8") if path.exists() else ""
    return result


class PromptRenderError(Exception):
    pass


async def prepare_prompt(
    path: Path,
    args: dict[str, str],
    exec_fn,
) -> str:
    """Read template, expand !`shell` expressions, then substitute {{placeholders}}.

    Substituted values are inert — never re-scanned for shell expressions, so
    diffs, issue bodies, or other user-influenced content cannot trigger
    container-side command execution.
    """
    content = path.read_text(encoding="utf-8")
    preprocessed = await _preprocess(content, exec_fn)
    return _render(preprocessed, args)


def _render(template: str, args: dict[str, str]) -> str:
    found = set(PLACEHOLDER.findall(template))
    missing = found - args.keys()
    if missing:
        raise PromptRenderError(f"Missing prompt args: {missing}")
    for key in args.keys() - found:
        print(f"  [warn] arg '{key}' unused in prompt", file=sys.stderr)
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
    PER_ISSUE = frozenset(
        {"ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_BODY", "ISSUE_COMMENTS", "BRANCH"}
    )
    MERGE = frozenset({"BRANCHES"})
    PLAN = frozenset({"OPEN_ISSUES_JSON"})
    PREFLIGHT = frozenset({"CHECK_NAME", "COMMAND", "OUTPUT"})
    IMPROVE_SCAN = frozenset[str]()
    IMPROVE_SESSION = frozenset({"IMPROVE_SHORT_SID"})
    RESUME = frozenset[str]()

    @property
    def placeholders(self) -> frozenset[str]:
        return self.value  # type: ignore[return-value]


class PromptTemplate(enum.Enum):
    IMPLEMENT = ("implement-prompt.md", Scope.PER_ISSUE)
    REVIEW = ("review-prompt.md", Scope.PER_ISSUE)
    MERGE = ("merge-prompt.md", Scope.MERGE)
    PLAN = ("plan-prompt.md", Scope.PLAN)
    PREFLIGHT_ISSUE = ("preflight-issue.md", Scope.PREFLIGHT)
    IMPROVE_SCAN = ("improve/01-scan.md", Scope.IMPROVE_SCAN)
    IMPROVE_PRD = ("improve/02-prd.md", Scope.IMPROVE_SESSION)
    IMPROVE_ISSUES = ("improve/03-issues.md", Scope.IMPROVE_SESSION)
    IMPROVE_NO_CANDIDATE = ("improve/04-no-candidate-report.md", Scope.IMPROVE_SESSION)
    RESUME = ("_resume-prompt.md", Scope.RESUME)

    @property
    def filename(self) -> str:
        return self.value[0]  # type: ignore[index]

    @property
    def scope(self) -> Scope:
        return self.value[1]  # type: ignore[index]


def _format_feedback_commands(checks: Sequence[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


class PromptRenderer:
    def __init__(self, cfg: Config) -> None:
        self._prompts_dir: Path = cfg.prompts_dir
        self._global_args = self._build_global_args(cfg)
        self._validate_templates()

    def _build_global_args(self, cfg: Config) -> dict[str, str]:
        standards = load_standards(cfg.prompts_dir)
        checks = " && ".join(cmd for _, cmd in cfg.preflight_checks)
        return {
            "BUG_LABEL": cfg.bug_label,
            "ISSUE_LABEL": cfg.issue_label,
            "HITL_LABEL": cfg.hitl_label,
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
