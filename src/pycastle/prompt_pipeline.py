import asyncio
import re
import sys
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SHELL_EXPR = re.compile(r"!`([^`]+)`")


class PromptRenderError(Exception):
    pass


async def prepare_prompt(
    path: Path,
    args: dict[str, str],
    exec_fn,
) -> str:
    """Read, render {{placeholders}}, and expand !`shell` expressions."""
    content = path.read_text(encoding="utf-8")
    rendered = _render(content, args)
    return await _preprocess(rendered, exec_fn)


def _render(template: str, args: dict[str, str]) -> str:
    found = set(PLACEHOLDER.findall(template))
    missing = found - args.keys()
    if missing:
        raise PromptRenderError(f"Missing prompt args: {missing}")
    for key in args.keys() - found:
        print(f"  [warn] arg '{key}' unused in prompt", file=sys.stderr)
    return PLACEHOLDER.sub(lambda m: args[m.group(1)], template)


async def _preprocess(prompt: str, exec_fn) -> str:
    matches = list(SHELL_EXPR.finditer(prompt))
    if not matches:
        return prompt
    results = await asyncio.gather(*[exec_fn(m.group(1)) for m in matches])
    for match, out in zip(reversed(matches), reversed(list(results))):
        prompt = prompt[: match.start()] + out.rstrip("\n") + prompt[match.end() :]
    return prompt
