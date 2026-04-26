# pycastle

pycastle is a Python orchestrator for autonomous [Claude Code](https://claude.ai/code) agents running inside Docker containers. It is inspired by [sandcastle](https://github.com/mattpocock/sandcastle) — Matt Pocock's original project — and brings the same multi-agent, worktree-based workflow to a pip-installable Python package with configurable prompts, Dockerfile, and environment.

## Installation

```bash
pip install pycastle
```

## Prerequisites

- Python 3.11.3 or later
- Docker (daemon must be running)
- A valid `ANTHROPIC_API_KEY` environment variable (or a `.env` file in your project root)

## CLI Commands

### `pycastle init`

Copies the default `pycastle/` configuration directory into your project root. This directory contains the `Dockerfile`, `config.py`, and prompt templates (`plan-prompt.md`, `implement-prompt.md`, `review-prompt.md`, `merge-prompt.md`, `CODING_STANDARDS.md`) that drive the agents. Run this once per repository, then customise the files to suit your project.

```bash
pycastle init
```

### `pycastle build`

Builds the Docker image defined in `pycastle/Dockerfile`. Pass `--no-cache` to force a clean build. You must rebuild whenever you change the Dockerfile or install new dependencies.

```bash
pycastle build [--no-cache]
```

### `pycastle run`

Reads a GitHub issue number and orchestrates a full agent pipeline inside Docker: a planner drafts an implementation plan, one or more implementer agents write the code on isolated git worktrees, a reviewer checks each implementation, and a merger integrates the approved changes. Progress is streamed to your terminal in real time.

```bash
pycastle run
```

## Configuration

All runtime configuration lives in `pycastle/config.py`. Key settings include the GitHub repository, the Docker image name, agent model selection, and flags such as `skip_preflight`. Edit this file after running `pycastle init` to tailor the pipeline to your project.
