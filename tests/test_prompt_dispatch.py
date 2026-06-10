import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from pycastle.prompts.dispatch import (
    PromptInvocation,
    build_prompt_invocation,
    render_prompt_invocation,
)
from pycastle.prompts.pipeline import PromptRenderError, PromptTemplate
from pycastle.session import RunKind


class _RecordingRenderer:
    def __init__(self) -> None:
        self.calls: list[
            tuple[PromptTemplate, dict[str, str], Callable[[str], Awaitable[str]]]
        ] = []

    async def render(
        self,
        template: PromptTemplate,
        scope_args: dict[str, str],
        exec_fn: Callable[[str], Awaitable[str]],
    ) -> str:
        self.calls.append((template, scope_args, exec_fn))
        return f"{template.name}:{sorted(scope_args.items())}"


async def _noop_exec(cmd: str) -> str:
    del cmd
    return ""


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_prompt_invocation_rejects_missing_and_extra_role_scope_args() -> None:
    with pytest.raises(
        PromptRenderError,
        match="scope_args mismatch for template PLAN: missing:",
    ):
        PromptInvocation(
            template=PromptTemplate.PLAN,
            scope_args={"ALL_OPEN_ISSUES_JSON": "[]", "EXTRA_KEY": "oops"},
        )


def test_build_prompt_invocation_carries_validated_scope_args_and_resume_flag() -> None:
    invocation = build_prompt_invocation(
        PromptTemplate.PLAN,
        {
            "ALL_OPEN_ISSUES_JSON": "[]",
            "READY_FOR_AGENT_ISSUES_JSON": "[]",
        },
        send_role_prompt_on_resume=True,
    )

    assert invocation == PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": "[]",
            "READY_FOR_AGENT_ISSUES_JSON": "[]",
        },
        send_role_prompt_on_resume=True,
    )


def test_build_prompt_invocation_reuses_template_scope_validation() -> None:
    with pytest.raises(
        PromptRenderError,
        match="scope_args mismatch for template PLAN: missing:",
    ):
        build_prompt_invocation(
            PromptTemplate.PLAN,
            {"ALL_OPEN_ISSUES_JSON": "[]"},
        )


def test_render_prompt_invocation_uses_resume_template_when_resume_skips_role_prompt() -> (
    None
):
    renderer = _RecordingRenderer()
    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": "[]",
            "READY_FOR_AGENT_ISSUES_JSON": "[]",
        },
        send_role_prompt_on_resume=False,
    )

    result = asyncio.run(
        render_prompt_invocation(
            invocation,
            renderer=renderer,
            run_kind=RunKind.RESUME,
            exec_fn=_noop_exec,
        )
    )

    assert result == "RESUME:[]"
    assert renderer.calls == [(PromptTemplate.RESUME, {}, _noop_exec)]


def test_render_prompt_invocation_delegates_role_rendering_for_fresh_runs() -> None:
    renderer = _RecordingRenderer()
    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": "[]",
            "READY_FOR_AGENT_ISSUES_JSON": "[]",
        },
        send_role_prompt_on_resume=False,
    )

    result = asyncio.run(
        render_prompt_invocation(
            invocation,
            renderer=renderer,
            run_kind=RunKind.FRESH,
            exec_fn=_noop_exec,
        )
    )

    assert result == (
        "PLAN:[('ALL_OPEN_ISSUES_JSON', '[]'), ('READY_FOR_AGENT_ISSUES_JSON', '[]')]"
    )
    assert renderer.calls == [
        (
            PromptTemplate.PLAN,
            {
                "ALL_OPEN_ISSUES_JSON": "[]",
                "READY_FOR_AGENT_ISSUES_JSON": "[]",
            },
            _noop_exec,
        )
    ]
