import asyncio

import pytest

from pycastle.prompt_pipeline import PromptRenderError, prepare_prompt


async def _noop_exec(cmd: str) -> str:
    return f"output-of:{cmd}"


def run(coro):
    return asyncio.run(coro)


def test_renders_placeholder(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("Hello {{NAME}}!")
    assert run(prepare_prompt(f, {"NAME": "World"}, _noop_exec)) == "Hello World!"


def test_expands_shell_expression(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("Issues: !`gh issue list`")
    assert run(prepare_prompt(f, {}, _noop_exec)) == "Issues: output-of:gh issue list"


def test_renders_before_preprocess(tmp_path):
    """Placeholder substitution happens before shell expansion."""
    f = tmp_path / "p.md"
    f.write_text("!`echo {{MSG}}`")
    assert (
        run(prepare_prompt(f, {"MSG": "hello"}, _noop_exec)) == "output-of:echo hello"
    )


def test_raises_on_missing_arg(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("Hello {{NAME}}!")
    with pytest.raises(PromptRenderError):
        run(prepare_prompt(f, {}, _noop_exec))


def test_warns_on_unused_arg(tmp_path, capsys):
    f = tmp_path / "p.md"
    f.write_text("Hello!")
    run(prepare_prompt(f, {"UNUSED": "value"}, _noop_exec))
    assert "UNUSED" in capsys.readouterr().err


def test_strips_trailing_newline(tmp_path):
    async def exec_with_newline(cmd: str) -> str:
        return "output\n"

    f = tmp_path / "p.md"
    f.write_text("Result: !`cmd`")
    assert run(prepare_prompt(f, {}, exec_with_newline)) == "Result: output"


def test_no_transforms_returns_unchanged(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("Plain text, no magic.")
    assert run(prepare_prompt(f, {}, _noop_exec)) == "Plain text, no magic."


def test_multiple_placeholders_and_shells(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("{{GREETING}} !`cmd1` and !`cmd2`")
    result = run(prepare_prompt(f, {"GREETING": "Hi"}, _noop_exec))
    assert result == "Hi output-of:cmd1 and output-of:cmd2"
