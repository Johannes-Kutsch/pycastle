from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

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


@dataclass(frozen=True)
class ScaffoldArtifactReport:
    status: ScaffoldStatus
    path: str


@dataclass(frozen=True)
class InitScaffold:
    pycastle_dir: Path
    pycastle_home: Path
    defaults: Traversable

    def install_defaults(self) -> None:
        self.pycastle_dir.mkdir(parents=True, exist_ok=True)
        self._write_config_example(self.pycastle_dir)
        if (self.pycastle_home / "config.py.example").exists():
            self._write_config_example(self.pycastle_home)

        for rel in sorted(MANAGED_SCAFFOLD_ALLOWLIST):
            self._copy_default(rel, self.pycastle_dir / rel)

    def refresh(self) -> tuple[ScaffoldArtifactReport, ...]:
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

        for rel in ("config.py", ".env"):
            if (self.pycastle_dir / rel).exists():
                report.append(ScaffoldArtifactReport(status="preserved", path=rel))

        return tuple(report)

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

    def render_config_example(self) -> str:
        return _render_config_example((self.defaults / "config.py").read_text())

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
