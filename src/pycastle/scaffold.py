from __future__ import annotations

import ast
import re
import stat
from dataclasses import dataclass
from functools import cached_property
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal, overload

ScaffoldStatus = Literal["created", "overwrote", "unchanged", "preserved"]

MANAGED_SCAFFOLD_ALLOWLIST = frozenset(
    {
        ".gitignore",
        "setup/cron.sh",
        "setup/cron-install.sh",
        "setup/cron-uninstall.sh",
    }
)
_CONFIG_FIELD_RE = re.compile(r"[a-z_]+\s*=")
_EXCLUDED_CONFIG_EXAMPLE_FIELDS = frozenset({"auto_file_bugs", "bug_report_repo"})
_BUNDLED_DEFAULT_STAGE_OVERRIDE_NAMES = frozenset(
    {
        "plan_override",
        "implement_override",
        "review_override",
        "merge_override",
        "preflight_issue_override",
        "improve_override",
    }
)


@dataclass(frozen=True)
class ScaffoldArtifactReport:
    status: ScaffoldStatus
    path: str


@dataclass(frozen=True)
class ScaffoldRefreshReport:
    artifacts: tuple[ScaffoldArtifactReport, ...]

    def __iter__(self):
        return iter(self.artifacts)

    def __len__(self) -> int:
        return len(self.artifacts)

    @overload
    def __getitem__(self, index: int) -> ScaffoldArtifactReport: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[ScaffoldArtifactReport, ...]: ...

    def __getitem__(
        self, index: int | slice
    ) -> ScaffoldArtifactReport | tuple[ScaffoldArtifactReport, ...]:
        return self.artifacts[index]

    def display_lines(self) -> tuple[str, ...]:
        overwrote_paths = sorted(
            entry.path for entry in self.artifacts if entry.status == "overwrote"
        )
        if overwrote_paths:
            return tuple(f"overwrote {path}" for path in overwrote_paths)
        if self.is_up_to_date():
            return ("pycastle directory is already up to date.",)
        return ()

    def is_up_to_date(self) -> bool:
        return not any(
            entry.status in {"created", "overwrote"} for entry in self.artifacts
        )


@dataclass(frozen=True)
class _BundledDefaultsIntrospection:
    text: str
    tree: ast.Module

    @classmethod
    def from_defaults(cls, defaults: Traversable) -> _BundledDefaultsIntrospection:
        text = (defaults / "config.py").read_text()
        return cls(text=text, tree=ast.parse(text))

    def render_config_example(self) -> str:
        return _render_config_example(self.text)

    def bundled_default_stage_chains(self) -> tuple[tuple[str, ...], ...]:
        chains: list[tuple[str, ...]] = []
        for node in self.tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id in _BUNDLED_DEFAULT_STAGE_OVERRIDE_NAMES
                ):
                    chains.append(_parse_stage_override_services(node.value))
                    break
        return tuple(chains)


@dataclass(frozen=True)
class InitScaffold:
    pycastle_dir: Path
    pycastle_home: Path
    defaults: Traversable

    def install_defaults(self) -> None:
        self._apply_managed_scaffold(include_preserved=False)

    def refresh(self) -> ScaffoldRefreshReport:
        return ScaffoldRefreshReport(
            tuple(self._apply_managed_scaffold(include_preserved=True))
        )

    def _apply_managed_scaffold(
        self, *, include_preserved: bool
    ) -> list[ScaffoldArtifactReport]:
        self.pycastle_dir.mkdir(parents=True, exist_ok=True)

        config_example_path = self.pycastle_dir / "config.py.example"
        config_example_text = self.render_config_example()
        report: list[ScaffoldArtifactReport] = [
            ScaffoldArtifactReport(
                status=self._text_status(config_example_path, config_example_text),
                path="config.py.example",
            )
        ]
        self._write_config_example(self.pycastle_dir)
        home_config_example_path = self.pycastle_home / "config.py.example"
        if home_config_example_path.exists():
            report.append(
                ScaffoldArtifactReport(
                    status=self._text_status(
                        home_config_example_path, config_example_text
                    ),
                    path="pycastle home/config.py.example",
                )
            )
            self._write_config_example(self.pycastle_home)

        for rel in sorted(MANAGED_SCAFFOLD_ALLOWLIST):
            target = self.pycastle_dir / rel
            report.append(
                ScaffoldArtifactReport(
                    status=self._default_status(rel, target),
                    path=rel,
                )
            )
            self._copy_default(rel, target)

        if include_preserved:
            for rel in ("config.py", ".env"):
                if (self.pycastle_dir / rel).exists():
                    report.append(ScaffoldArtifactReport(status="preserved", path=rel))

        return report

    def _default_path(self, rel: str) -> Traversable:
        default = self.defaults
        for part in rel.split("/"):
            default = default / part
        return default

    def _copy_default(self, rel: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._default_path(rel).read_bytes())
        if target.suffix == ".sh":
            target.chmod(
                target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

    @cached_property
    def _bundled_defaults(self) -> _BundledDefaultsIntrospection:
        return _BundledDefaultsIntrospection.from_defaults(self.defaults)

    def render_config_example(self) -> str:
        return self._bundled_defaults.render_config_example()

    def bundled_default_stage_chains(self) -> tuple[tuple[str, ...], ...]:
        return self._bundled_defaults.bundled_default_stage_chains()

    def _write_config_example(self, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "config.py.example").write_text(self.render_config_example())

    def _default_status(self, rel: str, target: Path) -> ScaffoldStatus:
        if not target.exists():
            return "created"
        return (
            "unchanged"
            if target.read_bytes() == self._default_path(rel).read_bytes()
            else "overwrote"
        )

    @staticmethod
    def _text_status(target: Path, expected: str) -> ScaffoldStatus:
        if not target.exists():
            return "created"
        return "unchanged" if target.read_text() == expected else "overwrote"


def _parse_stage_override_services(node: ast.AST) -> tuple[str, ...]:
    if not isinstance(node, ast.Call):
        return ()

    service = ""
    fallback_services: tuple[str, ...] = ()
    for keyword in node.keywords:
        if keyword.arg == "service" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                service = keyword.value.value
        if keyword.arg == "fallback":
            fallback_services = _parse_stage_override_services(keyword.value)

    services = [service] if service else []
    services.extend(fallback_services)
    return tuple(services)


def _render_config_example(defaults_text: str) -> str:
    out = ["from pathlib import Path", ""]
    uncomment_block = False
    preserve_commented_block = False

    for line in defaults_text.splitlines():
        if line == "from pathlib import Path":
            continue
        if line.startswith("# "):
            body = line[2:]
            if uncomment_block:
                out.append(body)
                if body.strip() == ")":
                    uncomment_block = False
                continue
            if preserve_commented_block:
                out.append(line)
                if body.strip() == ")":
                    preserve_commented_block = False
                continue
            if _CONFIG_FIELD_RE.match(body):
                field_name = body.split("=", 1)[0].strip()
                if field_name in _EXCLUDED_CONFIG_EXAMPLE_FIELDS:
                    continue
                if body.startswith("opencode_") or (
                    body.startswith("plan_override")
                    and 'model="kimi-k2.6"' in body
                    and 'service="opencode"' in body
                ):
                    out.append(line)
                    preserve_commented_block = body.rstrip().endswith("(")
                else:
                    out.append(body)
                    uncomment_block = body.rstrip().endswith("(")
                continue
        out.append(line)

    return "\n".join(out).rstrip() + "\n"
