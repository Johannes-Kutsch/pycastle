from __future__ import annotations

from collections.abc import Callable, Iterable

from .contracts import (
    AssistantTurn,
    CredentialFailure,
    HardError,
    ParsedTurn,
    PromptTokens,
    Result,
    TransientError,
    UnsupportedTokens,
    UsageLimit,
)
from .errors import (
    AgentCredentialFailureError,
    HardAgentError,
    TransientAgentError,
    UsageLimitError,
)


def reduce_provider_failure(
    event: UsageLimit | TransientError | HardError | CredentialFailure,
    *,
    provider: str | None = None,
) -> None:
    if isinstance(event, UsageLimit):
        raise UsageLimitError(
            reset_time=event.reset_time,
            raw_message=event.raw_message,
            provider=provider,
            is_permanent=event.is_permanent,
        )
    if isinstance(event, TransientError):
        raise TransientAgentError(
            message=event.raw_message,
            status_code=event.status_code,
        )
    if isinstance(event, HardError):
        raise HardAgentError(
            message=event.raw_message,
            status_code=event.status_code,
            service_name=provider or "",
            classification=event.classification,
            observations=event.observations,
        )
    raise AgentCredentialFailureError(
        message=event.raw_message,
        status_code=event.status_code,
        service_name=event.service_name,
        classification=event.classification,
        observations=event.source_observations,
    )


def reduce_successful_text_output_events(
    events: Iterable[ParsedTurn],
    on_turn: Callable[[str], None],
    on_tokens: Callable[[int], None] | None = None,
) -> str:
    reducer = _SuccessfulTextOutputReducer(on_turn, on_tokens)
    for event in events:
        if reducer.consume(event):
            break
    return reducer.finish()


def reduce_text_output_events(
    events: Iterable[ParsedTurn],
    on_turn: Callable[[str], None],
    on_tokens: Callable[[int], None] | None = None,
    *,
    provider: str,
) -> str:
    reducer = _SuccessfulTextOutputReducer(on_turn, on_tokens)
    for event in events:
        if isinstance(
            event, (UsageLimit, TransientError, HardError, CredentialFailure)
        ):
            reduce_provider_failure(event, provider=provider)
        if reducer.consume(event):
            break
    return reducer.finish()


class _SuccessfulTextOutputReducer:
    def __init__(
        self,
        on_turn: Callable[[str], None],
        on_tokens: Callable[[int], None] | None,
    ) -> None:
        self._on_turn = on_turn
        self._on_tokens = on_tokens
        self._result_text: str | None = None
        self._collected_turns: list[str] = []

    def consume(self, event: ParsedTurn) -> bool:
        if isinstance(event, PromptTokens):
            if self._on_tokens is not None:
                self._on_tokens(event.count)
            return False
        if isinstance(event, UnsupportedTokens):
            return False
        if isinstance(event, AssistantTurn):
            self._on_turn(event.text)
            self._collected_turns.append(event.text)
            return False
        if isinstance(event, Result):
            self._result_text = event.text
            return True
        return False

    def finish(self) -> str:
        if self._result_text is not None:
            return self._result_text
        return "\n".join(self._collected_turns)
