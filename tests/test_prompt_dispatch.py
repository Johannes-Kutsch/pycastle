import asyncio
from pathlib import Path

import pytest

from pycastle.config import Config
from pycastle.prompts.dispatch import (
    PromptInvocation,
    build_prompt_invocation,
    render_prompt_invocation,
)
from pycastle.prompts.pipeline import PromptRenderError, PromptRenderer, PromptTemplate
from pycastle.session import RunKind


async def _noop_exec(cmd: str) -> str:
    del cmd
    return ""


async def _echo_exec(cmd: str) -> str:
    return f"exec:{cmd}"


@pytest.fixture(autouse=True)
def _project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    prompts_dir = tmp_path / "pycastle" / "prompts"
    (prompts_dir / "coordination").mkdir(parents=True)
    (prompts_dir / "shared").mkdir()
    return prompts_dir


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


def test_fresh_prompt_dispatch_renders_role_prompt_with_validated_scope_args(
    prompts_dir: Path,
) -> None:
    (prompts_dir / "coordination" / "plan.md").write_text(
        "Open={{ALL_OPEN_ISSUES_JSON}}\nReady={{READY_FOR_AGENT_ISSUES_JSON}}\n"
    )
    renderer = PromptRenderer(Config())
    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": '[{"number": 1}]',
            "READY_FOR_AGENT_ISSUES_JSON": '[{"number": 1}]',
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

    assert result == ('Open=[{"number": 1}]\nReady=[{"number": 1}]\n')


def test_resume_prompt_dispatch_uses_resume_template_when_role_prompt_is_disabled(
    prompts_dir: Path,
) -> None:
    (prompts_dir / "coordination" / "plan.md").write_text(
        "role={{ALL_OPEN_ISSUES_JSON}}"
    )
    (prompts_dir / "shared" / "resume.md").write_text("resume prompt")
    renderer = PromptRenderer(Config())
    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": '[{"number": 1}]',
            "READY_FOR_AGENT_ISSUES_JSON": '[{"number": 1}]',
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

    assert result == "resume prompt"


def test_resume_prompt_dispatch_renders_role_prompt_when_role_prompt_is_enabled(
    prompts_dir: Path,
) -> None:
    (prompts_dir / "coordination" / "plan.md").write_text(
        "role={{ALL_OPEN_ISSUES_JSON}}"
    )
    (prompts_dir / "shared" / "resume.md").write_text("resume prompt")
    renderer = PromptRenderer(Config())
    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": '[{"number": 1}]',
            "READY_FOR_AGENT_ISSUES_JSON": '[{"number": 2}]',
        },
        send_role_prompt_on_resume=True,
    )

    result = asyncio.run(
        render_prompt_invocation(
            invocation,
            renderer=renderer,
            run_kind=RunKind.RESUME,
            exec_fn=_noop_exec,
        )
    )

    assert result == 'role=[{"number": 1}]'


def test_prompt_dispatch_runs_shell_expression_through_supplied_executor(
    prompts_dir: Path,
) -> None:
    (prompts_dir / "coordination" / "plan.md").write_text(
        "shell=!`printf prompt-dispatch`\nReady={{READY_FOR_AGENT_ISSUES_JSON}}"
    )
    renderer = PromptRenderer(Config())
    invocation = PromptInvocation(
        template=PromptTemplate.PLAN,
        scope_args={
            "ALL_OPEN_ISSUES_JSON": "[]",
            "READY_FOR_AGENT_ISSUES_JSON": "[ready]",
        },
    )

    result = asyncio.run(
        render_prompt_invocation(
            invocation,
            renderer=renderer,
            run_kind=RunKind.FRESH,
            exec_fn=_echo_exec,
        )
    )

    assert result == "shell=exec:printf prompt-dispatch\nReady=[ready]"
