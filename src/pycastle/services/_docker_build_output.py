from __future__ import annotations

import enum
import re


class BuildOutcome(enum.Enum):
    REBUILT = "rebuilt"
    FULL_CACHE_HIT = "full_cache_hit"


def interpret_final_build_outcome(output: str) -> BuildOutcome:
    if _is_full_cache_hit(output):
        return BuildOutcome.FULL_CACHE_HIT
    return BuildOutcome.REBUILT


def _is_full_cache_hit(output: str) -> bool:
    lines = output.splitlines()

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
