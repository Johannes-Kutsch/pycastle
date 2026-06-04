from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


class BuildOutcome(enum.Enum):
    REBUILT = "rebuilt"
    FULL_CACHE_HIT = "full_cache_hit"


@dataclass(frozen=True)
class BuildLineInterpretation:
    rebuild_started: bool = False
    progress_text: str | None = None


@dataclass(frozen=True)
class FinalOutcomeExample:
    lines: tuple[str, ...]
    outcome: BuildOutcome


FINAL_OUTCOME_EXAMPLES: dict[str, FinalOutcomeExample] = {
    "buildkit_all_cached": FinalOutcomeExample(
        lines=(
            "#1 [1/2] FROM python:3.12\n",
            "#1 CACHED\n",
            "#2 [2/2] RUN pip install requests\n",
            "#2 CACHED\n",
        ),
        outcome=BuildOutcome.FULL_CACHE_HIT,
    ),
    "buildkit_rebuilt": FinalOutcomeExample(
        lines=(
            "#1 [1/2] FROM python:3.12\n",
            "#1 CACHED\n",
            "#2 [2/2] COPY . .\n",
            "#2 DONE 2.5s\n",
        ),
        outcome=BuildOutcome.REBUILT,
    ),
    "classic_all_cached": FinalOutcomeExample(
        lines=(
            "Step 1/2 : FROM python:3.12\n",
            " ---> Using cache\n",
            " ---> abc123\n",
            "Step 2/2 : RUN pip install requests\n",
            " ---> Using cache\n",
            " ---> def456\n",
            "Successfully built def456\n",
        ),
        outcome=BuildOutcome.FULL_CACHE_HIT,
    ),
    "classic_mixed": FinalOutcomeExample(
        lines=(
            "Step 1/2 : FROM python:3.12\n",
            " ---> Using cache\n",
            " ---> abc123\n",
            "Step 2/2 : COPY . .\n",
            " ---> Running in 789abc\n",
            "Successfully built 789abc\n",
        ),
        outcome=BuildOutcome.REBUILT,
    ),
}


@dataclass
class DockerBuildOutputInterpreter:
    on_rebuild_start: Callable[[], None] | None = None
    _classic_steps_seen: int = 0
    _classic_steps_cached: int = 0
    _pending_classic_step: bool = False
    _buildkit_step_ids: set[str] = field(default_factory=set)
    _has_buildkit_cached: bool = False
    _has_buildkit_done: bool = False
    _rebuild_started: bool = False
    _buildkit_total_steps: int | None = None
    _last_progress_text: str | None = None

    def observe_line(self, line: str) -> BuildLineInterpretation:
        stripped = line.strip()
        buildkit_step = re.match(r"^#(\d+)\s+\[(\d+)/(\d+)\]", stripped)
        if buildkit_step:
            self._buildkit_step_ids.add(buildkit_step.group(1))
            if self._buildkit_total_steps is None:
                self._buildkit_total_steps = int(buildkit_step.group(3))
            return self._progress(
                f"Step {buildkit_step.group(2)}/{self._buildkit_total_steps}"
            )

        classic_step = re.match(r"^Step (\d+)/(\d+) :", line)
        if classic_step:
            self._classic_steps_seen += 1
            self._pending_classic_step = True
            return self._progress(
                f"Step {classic_step.group(1)}/{classic_step.group(2)}"
            )

        if self._pending_classic_step:
            if not stripped:
                return BuildLineInterpretation()
            self._pending_classic_step = False
            if "---> Using cache" in stripped:
                self._classic_steps_cached += 1
                return BuildLineInterpretation()
            if stripped:
                return self._mark_rebuild_started()

        buildkit_cached = re.match(r"^#(\d+)\s+CACHED\s*$", stripped)
        if buildkit_cached and buildkit_cached.group(1) in self._buildkit_step_ids:
            self._has_buildkit_cached = True
            return BuildLineInterpretation()

        buildkit_done = re.match(r"^#(\d+)\s+DONE\s+", stripped)
        if buildkit_done and buildkit_done.group(1) in self._buildkit_step_ids:
            self._has_buildkit_done = True
            return self._mark_rebuild_started()

        if self._last_progress_text is not None and re.match(
            r"^#\d+\s+(exporting|naming|unpacking|manifest|pushing)", stripped
        ):
            return self._progress("exporting…")

        return BuildLineInterpretation()

    @property
    def final_outcome(self) -> BuildOutcome:
        if self._classic_steps_seen:
            if self._classic_steps_seen == self._classic_steps_cached:
                return BuildOutcome.FULL_CACHE_HIT
            return BuildOutcome.REBUILT

        if self._has_buildkit_cached and not self._has_buildkit_done:
            return BuildOutcome.FULL_CACHE_HIT
        return BuildOutcome.REBUILT

    def _mark_rebuild_started(self) -> BuildLineInterpretation:
        if self._rebuild_started:
            return BuildLineInterpretation()
        self._rebuild_started = True
        if self.on_rebuild_start is not None:
            self.on_rebuild_start()
        return BuildLineInterpretation(rebuild_started=True)

    def _progress(self, text: str) -> BuildLineInterpretation:
        if text == self._last_progress_text:
            return BuildLineInterpretation()
        self._last_progress_text = text
        return BuildLineInterpretation(progress_text=text)


def interpret_final_build_outcome(output: str | Iterable[str]) -> BuildOutcome:
    interpreter = DockerBuildOutputInterpreter()
    for line in _output_lines(output):
        interpreter.observe_line(line)
    return interpreter.final_outcome


def _output_lines(output: str | Iterable[str]) -> list[str]:
    if isinstance(output, str):
        return output.splitlines()
    return list(output)
