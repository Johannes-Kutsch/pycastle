from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pycastle.prompts.pipeline import PromptRenderer, PromptTemplate
from pycastle.prompts.scope_args import validated_scope_args_for_template
from pycastle.session import RunKind


class PromptDispatchRenderer(Protocol):
    async def render(
        self,
        template: PromptTemplate,
        scope_args: dict[str, str],
        exec_fn: Callable[[str], Awaitable[str]],
    ) -> str: ...


@dataclass(frozen=True)
class PromptInvocation:
    template: PromptTemplate
    scope_args: dict[str, str]
    send_role_prompt_on_resume: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scope_args",
            validated_scope_args_for_template(self.template, self.scope_args),
        )


def build_prompt_invocation(
    template: PromptTemplate,
    scope_args: dict[str, str],
    *,
    send_role_prompt_on_resume: bool = False,
) -> PromptInvocation:
    return PromptInvocation(
        template=template,
        scope_args=validated_scope_args_for_template(template, scope_args),
        send_role_prompt_on_resume=send_role_prompt_on_resume,
    )


async def render_prompt_invocation(
    invocation: PromptInvocation,
    *,
    renderer: PromptRenderer | PromptDispatchRenderer,
    run_kind: RunKind,
    exec_fn: Callable[[str], Awaitable[str]],
) -> str:
    if run_kind is RunKind.RESUME and not invocation.send_role_prompt_on_resume:
        return await renderer.render(PromptTemplate.RESUME, {}, exec_fn)
    return await renderer.render(invocation.template, invocation.scope_args, exec_fn)


__all__ = [
    "PromptDispatchRenderer",
    "PromptInvocation",
    "build_prompt_invocation",
    "render_prompt_invocation",
]
