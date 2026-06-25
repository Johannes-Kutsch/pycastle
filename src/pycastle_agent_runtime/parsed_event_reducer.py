from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from .agent_service import (
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

if TYPE_CHECKING:
    pass

OutputT = TypeVar("OutputT")


def _identity(value: OutputT) -> OutputT:
    return value


def reduce_provider_failure(
    event: UsageLimit | TransientError | HardError | CredentialFailure,
    *,
    provider: str | None = None,
) -> None:
    from .errors import (
        AgentCredentialFailureError,
        HardAgentError,
        TransientAgentError,
        UsageLimitError,
    )

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
    *,
    extract_early_output: Callable[[str], OutputT | None] | None = None,
    extract_final_output: Callable[[str], OutputT] | None = None,
    post_process_output: Callable[[OutputT, str], OutputT] | None = None,
) -> OutputT | str:
    reducer = _SuccessfulTextOutputReducer(
        on_turn,
        on_tokens,
        extract_early_output=extract_early_output,
        extract_final_output=extract_final_output,
        post_process_output=post_process_output,
    )
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
    extract_early_output: Callable[[str], OutputT | None] | None = None,
    extract_final_output: Callable[[str], OutputT] | None = None,
    post_process_output: Callable[[OutputT, str], OutputT] | None = None,
) -> OutputT | str:
    reducer = _SuccessfulTextOutputReducer(
        on_turn,
        on_tokens,
        extract_early_output=extract_early_output,
        extract_final_output=extract_final_output,
        post_process_output=post_process_output,
    )
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
        *,
        extract_early_output: Callable[[str], OutputT | None] | None,
        extract_final_output: Callable[[str], OutputT] | None,
        post_process_output: Callable[[OutputT, str], OutputT] | None,
    ) -> None:
        self._on_turn = on_turn
        self._on_tokens = on_tokens
        self._result_text: str | None = None
        self._collected_turns: list[str] = []
        self._early_output: Any = None
        self._extract_early_output = extract_early_output
        self._extract_final_output: Callable[[str], Any] = (
            extract_final_output or _identity
        )
        self._post_process_output: Callable[[Any, str], Any] | None = (
            post_process_output
        )

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
            if self._extract_early_output is not None:
                self._early_output = self._extract_early_output(event.text)
                if self._early_output is not None:
                    return True
            return False
        if isinstance(event, Result):
            self._result_text = event.text
            return True
        return False

    def finish(self) -> OutputT | str:
        transcript = "\n".join(self._collected_turns)
        if self._early_output is not None:
            result: Any = self._early_output
        else:
            text = self._result_text if self._result_text is not None else transcript
            result = self._extract_final_output(text)
        if self._post_process_output is None:
            return cast(OutputT | str, result)
        return cast(OutputT | str, self._post_process_output(result, transcript))
