import dataclasses
from collections.abc import Callable

from agent_runtime.errors import HardAgentError

from pycastle.errors import (
    AgentFailedError,
    ModelNotAvailableError,
    TransientAgentError,
    UsageLimitError,
)
from pycastle.iteration.implement import ImplementResult

_FATAL_PRIORITY: tuple[type[Exception], ...] = (
    AgentFailedError,
    HardAgentError,
    TransientAgentError,
    ModelNotAvailableError,
)


@dataclasses.dataclass(frozen=True)
class ClassifyFatal:
    exception: Exception


@dataclasses.dataclass(frozen=True)
class ClassifySuccess:
    result: ImplementResult


def classify(
    issues: list[dict],
    results: list,
    *,
    log_error: Callable[[dict, Exception], None],
) -> ClassifyFatal | ClassifySuccess:
    best: Exception | None = None
    best_rank = len(_FATAL_PRIORITY)
    for result in results:
        if not isinstance(result, Exception):
            continue
        for rank, cls in enumerate(_FATAL_PRIORITY):
            if isinstance(result, cls) and rank < best_rank:
                best = result
                best_rank = rank
                break
    if best is not None:
        return ClassifyFatal(exception=best)

    usage_limit_errors = [r for r in results if isinstance(r, UsageLimitError)]
    usage_limit_hit = bool(usage_limit_errors)
    usage_limit_reset_time = next(
        (e.reset_time for e in usage_limit_errors if e.reset_time is not None),
        None,
    )
    first = usage_limit_errors[0] if usage_limit_errors else None

    completed: list[dict] = []
    errors: list[tuple[dict, Exception]] = []
    for issue, result in zip(issues, results, strict=False):
        if isinstance(result, UsageLimitError):
            continue
        if isinstance(result, Exception):
            log_error(issue, result)
            errors.append((issue, result))
        elif isinstance(result, dict):
            completed.append(issue)

    return ClassifySuccess(
        result=ImplementResult(
            completed=completed,
            errors=errors,
            usage_limit_hit=usage_limit_hit,
            usage_limit_reset_time=usage_limit_reset_time,
            usage_limit_provider=first.provider if first else None,
            usage_limit_raw_message=first.raw_message if first else None,
            usage_limit_account_label=first.account_label if first else None,
            usage_limit_is_permanent=first.is_permanent if first else False,
        )
    )
