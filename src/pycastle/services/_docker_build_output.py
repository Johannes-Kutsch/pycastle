from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass


class BuildOutcome(enum.Enum):
    REBUILT = "rebuilt"
    FULL_CACHE_HIT = "full_cache_hit"


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


def interpret_final_build_outcome(output: str | Iterable[str]) -> BuildOutcome:
    if _is_full_cache_hit(output):
        return BuildOutcome.FULL_CACHE_HIT
    return BuildOutcome.REBUILT


def _is_full_cache_hit(output: str | Iterable[str]) -> bool:
    lines = _output_lines(output)

    # Classic builder: look for Step N/M lines and check each for ---> Using cache
    classic_steps = [
        i for i, line in enumerate(lines) if re.match(r"^Step \d+/\d+ :", line)
    ]
    if classic_steps:
        for i in classic_steps:
            cached = any(
                "---> Using cache" in lines[j]
                for j in range(i + 1, min(i + 5, len(lines)))
                if not re.match(r"^Step \d+/\d+ :", lines[j])
            )
            if not cached:
                return False
        return True

    # BuildKit: CACHED means cached, DONE means executed (rebuilt)
    has_cached = any(re.match(r"^#\d+\s+CACHED\s*$", line.strip()) for line in lines)
    has_done = any(re.match(r"^#\d+\s+DONE\s+", line.strip()) for line in lines)

    return has_cached and not has_done


def _output_lines(output: str | Iterable[str]) -> list[str]:
    if isinstance(output, str):
        return output.splitlines()
    return list(output)
