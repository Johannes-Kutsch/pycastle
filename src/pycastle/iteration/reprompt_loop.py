from collections.abc import Awaitable, Callable

from ..agents.output_protocol import AgentOutput, AgentOutputProtocolError, FailedOutput

REPROMPT_MESSAGE = (
    "Your last response did not include the required protocol output. "
    "Please review the task requirements and try again, making sure to "
    "include the required output tag."
)

_DEFAULT_BUDGET = 3


async def run_with_reprompt(
    work_factory: Callable[[str | None], Awaitable[AgentOutput]],
    reprompt_message: str,
    budget: int = _DEFAULT_BUDGET,
) -> AgentOutput:
    """Drive a bounded retry loop until a valid protocol output is produced.

    Calls ``work_factory(None)`` for the first attempt and
    ``work_factory(reprompt_message)`` for each subsequent retry.
    Returns the first successful ``AgentOutput`` (including ``FailedOutput``
    when the agent signals failure explicitly).  After ``budget`` attempts
    without a successful parse, returns a synthesized ``FailedOutput``.
    """
    msg: str | None = None
    for _ in range(budget):
        try:
            return await work_factory(msg)
        except AgentOutputProtocolError:
            msg = reprompt_message
    return FailedOutput(failure_class="protocol_error")
