from __future__ import annotations

import asyncio
import enum
import re
from collections.abc import Sequence
from pathlib import Path

from ..config import Config
from ..session import RunKind
from .source import EffectivePromptFile, PromptSource

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SHELL_EXPR = re.compile(r"!`([^`]+)`")
CONDITIONAL_BLOCK = re.compile(
    r"\{\{#if\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s}]+)\s*\}\}(.*?)\{\{/if\}\}",
    re.DOTALL,
)


class PromptRenderError(Exception):
    pass


def _render(template: str, args: dict[str, str]) -> str:
    template = CONDITIONAL_BLOCK.sub(
        lambda m: m.group(3) if args.get(m.group(1)) == m.group(2) else "",
        template,
    )
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
            {
                "ISSUE_NUMBER",
                "ISSUE_TITLE",
                "ISSUE_BODY",
                "ISSUE_COMMENTS",
                "BRANCH",
                "INTERRUPTED_WORK",
            }
        ),
    )
    MERGE = ("MERGE", frozenset({"BRANCHES"}))
    PLAN = ("PLAN", frozenset({"ALL_OPEN_ISSUES_JSON", "READY_FOR_AGENT_ISSUES_JSON"}))
    PREFLIGHT = ("PREFLIGHT", frozenset({"CHECK_NAME", "COMMAND", "OUTPUT"}))
    HOST_CHECK = (
        "HOST_CHECK",
        frozenset(
            {
                "HOST_OS",
                "HOST_PLATFORM",
                "CHECKED_SHA",
                "CHECK_NAME",
                "COMMAND",
                "OUTPUT",
            }
        ),
    )
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
    DIVERGE = ("DIVERGE", frozenset({"BRANCH"}))
    RESUME = ("RESUME", frozenset[str]())
    FAILURE_REPORT = (
        "FAILURE_REPORT",
        frozenset({"FAILED_ROLE", "SESSION_DIR", "FAILURE_CLASS"}),
    )

    @property
    def placeholders(self) -> frozenset[str]:
        return self.value[1]  # type: ignore[index,no-any-return]


class PromptTemplate(enum.Enum):
    IMPLEMENT_BEHAVIOR = ("implement/behavior.md", Scope.PER_ISSUE)
    IMPLEMENT_REFACTOR = ("implement/refactor.md", Scope.PER_ISSUE)
    IMPLEMENT_DOCS = ("implement/docs.md", Scope.PER_ISSUE)
    REVIEW = ("review-prompt.md", Scope.PER_ISSUE)
    MERGE = ("merge-prompt.md", Scope.MERGE)
    PLAN = ("plan-prompt.md", Scope.PLAN)
    PREFLIGHT_ISSUE = ("preflight-issue.md", Scope.PREFLIGHT)
    HOST_CHECK_ISSUE = ("host-check-issue.md", Scope.HOST_CHECK)
    IMPROVE_SCAN = ("improve/01-scan.md", Scope.IMPROVE_SCAN)
    IMPROVE_PRD = ("improve/02-prd.md", Scope.IMPROVE_SESSION)
    IMPROVE_ISSUES = ("improve/03-issues.md", Scope.IMPROVE_ISSUES)
    IMPROVE_NO_CANDIDATE = ("improve/04-no-candidate-report.md", Scope.IMPROVE_SESSION)
    RESUME = ("_resume-prompt.md", Scope.RESUME)
    DIVERGENCE_RESOLVE = ("diverge-prompt.md", Scope.DIVERGE)
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


def build_interrupted_work_clause(run_kind: RunKind, is_dirty: bool) -> str:
    """Return interrupted-work instructions for fresh dispatches on dirty worktrees."""
    if run_kind != RunKind.FRESH or not is_dirty:
        return ""
    return (
        "\n# Interrupted Work\n\n"
        "This worktree has uncommitted changes from a previous agent run. "
        "Run `git diff` and `git status` to understand the current state, "
        "then continue from where the previous agent left off.\n"
    )


def _format_feedback_commands(checks: Sequence[str]) -> str:
    wrapped = [f"`{cmd}`" for cmd in checks]
    if len(wrapped) <= 1:
        return "".join(wrapped)
    return ", ".join(wrapped[:-1]) + " and " + wrapped[-1]


class PromptRenderer:
    _STATIC_SHARED_FILES: dict[str, str] = {
        "DESIGN_STANDARDS": "design.md",
        "IMPLEMENTATION_STANDARDS": "implementation.md",
        "IMPLEMENT_OUTPUT_RULES": "implement-output-rules.md",
        "ISSUE_TRACKER": "_issue-tracker.md",
    }
    _DYNAMIC_SHARED_FILES: dict[str, str] = {
        "IMPLEMENT_REVIEW_SHARED_FRAMING": "_implement-review-shared-framing.md",
    }

    def __init__(self, cfg: Config) -> None:
        prompts_dir = (
            Path("pycastle/prompts") if isinstance(cfg, Config) else cfg.prompts_dir
        )
        self._prompt_source = PromptSource.for_prompts_dir(prompts_dir)
        self._global_args = self._build_global_args(cfg)
        self._validate_templates()

    def _render_effective_file(
        self,
        prompt_file: EffectivePromptFile | None,
        *,
        allowed_args: dict[str, str],
        required: bool,
    ) -> str | None:
        if prompt_file is None:
            if required:
                raise PromptRenderError("Missing prompt fragment")
            return None
        content = prompt_file.read_text()
        found = set(PLACEHOLDER.findall(content))
        found |= {m.group(1) for m in CONDITIONAL_BLOCK.finditer(content)}
        unknown = found - allowed_args.keys()
        if unknown:
            raise PromptRenderError(
                "Prompt fragment "
                f"{prompt_file.relative_path!r} references unknown token(s): {unknown}"
            )
        return _render(content, allowed_args)

    def _load_static_shared_files(self, base_args: dict[str, str]) -> dict[str, str]:
        result = {}
        for key, filename in self._STATIC_SHARED_FILES.items():
            if filename.startswith("_"):
                rendered = self._render_effective_file(
                    self._prompt_source.maybe_lookup(filename),
                    allowed_args=base_args,
                    required=False,
                )
                if rendered is not None:
                    result[key] = rendered
            else:
                rendered = self._render_effective_file(
                    self._prompt_source.maybe_lookup(f"coding-standards/{filename}"),
                    allowed_args=base_args,
                    required=False,
                )
                result[key] = rendered or ""
        return result

    @staticmethod
    def _validation_args(
        global_args: dict[str, str], scope_placeholders: frozenset[str]
    ) -> dict[str, str]:
        return {
            **global_args,
            **{placeholder: "" for placeholder in scope_placeholders},
        }

    def _build_global_args(self, cfg: Config) -> dict[str, str]:
        checks = " && ".join(cmd for _, cmd in cfg.preflight_checks)
        base_args = {
            "BUG_LABEL": cfg.bug_label,
            "READY_FOR_AGENT_LABEL": cfg.issue_label,
            "READY_FOR_HUMAN_LABEL": cfg.hitl_label,
            "ENHANCEMENT_LABEL": cfg.enhancement_label,
            "NEEDS_TRIAGE_LABEL": cfg.needs_triage_label,
            "NEEDS_INFO_LABEL": cfg.needs_info_label,
            "WONTFIX_LABEL": cfg.wontfix_label,
            "REFACTOR_SLICE_LABEL": cfg.refactor_slice_label,
            "BEHAVIOR_SLICE_LABEL": cfg.behavior_slice_label,
            "DOCS_SLICE_LABEL": cfg.docs_slice_label,
            "FEEDBACK_COMMANDS": _format_feedback_commands(cfg.implement_checks),
            "CHECKS": checks,
        }
        return {**base_args, **self._load_static_shared_files(base_args)}

    def _validate_templates(self) -> None:
        global_keys = set(self._global_args.keys()) | set(self._DYNAMIC_SHARED_FILES)
        for template in PromptTemplate:
            prompt_file = self._prompt_source.maybe_lookup(template.filename)
            if prompt_file is None:
                continue
            content = prompt_file.read_text()
            found = set(PLACEHOLDER.findall(content))
            found |= {m.group(1) for m in CONDITIONAL_BLOCK.finditer(content)}
            allowed = global_keys | template.scope.placeholders
            unknown = found - allowed
            if unknown:
                raise PromptRenderError(
                    f"Template {template.filename!r} references unknown token(s): {unknown}"
                )
            validation_args = self._validation_args(
                self._global_args, template.scope.placeholders
            )
            for key, filename in self._DYNAMIC_SHARED_FILES.items():
                if key not in found:
                    continue
                rendered = self._render_effective_file(
                    self._prompt_source.maybe_lookup(filename),
                    allowed_args=validation_args,
                    required=False,
                )
                if rendered is None:
                    raise PromptRenderError(f"Missing prompt fragment: {filename}")

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

        content = self._prompt_source.lookup(template.filename).read_text()
        preprocessed = await _preprocess(content, exec_fn)
        all_args = {**self._global_args, **scope_args}
        for key, filename in self._DYNAMIC_SHARED_FILES.items():
            if key not in preprocessed:
                continue
            rendered = self._render_effective_file(
                self._prompt_source.maybe_lookup(filename),
                allowed_args=all_args,
                required=False,
            )
            if rendered is None:
                raise PromptRenderError(f"Missing prompt fragment: {filename}")
            all_args[key] = rendered
        return _render(preprocessed, all_args)
