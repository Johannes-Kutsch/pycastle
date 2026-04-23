# Ubiquitous Language

## Package & Distribution

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **pycastle** | The installable Python package that orchestrates Claude Code agents | orchestrator, tool |
| **consuming project** | A project that installs pycastle and customizes it via a local pycastle directory | host project, parent project |
| **pycastle directory** | The `pycastle/` directory inside a consuming project, containing local overrides for config, Dockerfile, .env, and prompts | config dir, override dir |
| **defaults** | The files bundled inside the pycastle package that serve as scaffolding templates and runtime fallbacks | base config, starter files |
| **pycastle init** | CLI command that copies all defaults into the consuming project's pycastle directory, then runs the init wizard | setup, scaffold |
| **init wizard** | The interactive step-by-step flow inside `pycastle init` that collects credentials and optionally creates GitHub labels | setup wizard, onboarding |
| **pycastle labels** | CLI subcommand that creates or resets GitHub labels in a target repo using the canonical label set | label setup, label sync |
| **auto-discovery** | The runtime behavior of looking for a pycastle directory in CWD before falling back to defaults | — |

## Configuration

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **config.py** | Python file in the pycastle directory defining behavioral configuration (paths, limits, image names) | settings.py, settings |
| **.env** | File in the pycastle directory holding secrets and credentials only — never committed to git | environment file, config |
| **GH_TOKEN** | GitHub personal access token stored in .env, used for GitHub API calls and label management | github token, gh pat |
| **CLAUDE_CODE_OAUTH_TOKEN** | Long-lived OAuth token for Claude Code authentication, generated via `claude setup-token` and stored in .env | claude token, oauth token |
| **ANTHROPIC_API_KEY** | Alternative Claude Code authentication via direct API key; not required when CLAUDE_CODE_OAUTH_TOKEN is set | api key, anthropic token |
| **CLAUDE_ACCOUNT_JSON** | Serialized Claude Code account credentials blob, read at runtime from `~/.claude.json` on the host — never stored in .env | claude config, claude json |
| **full replacement** | Override strategy where the local config.py replaces the package default entirely | merge, partial override |
| **config loader** | Package module that discovers and imports config.py from CWD, falling back to package defaults | — |

## GitHub Integration

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **label** | A GitHub issue classification marker with a name, description, and hex color | tag, category |
| **canonical label set** | The seven labels hardcoded in the pycastle package: `bug`, `enhancement`, `need-info`, `needs-triage`, `ready-for-agent`, `ready-for-human`, `wontfix` | default labels, label config |
| **label reset** | The option to delete all existing labels in a repo before creating the canonical label set | label wipe, clean labels |
| **issue label** | The specific label value (default: `ready-for-agent`) that marks a GitHub issue as eligible for agent processing | trigger label, agent label |

## Agents & Orchestration

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent** | A Claude Code instance running inside an isolated Docker container | bot, worker |
| **orchestrator** | The main loop that coordinates agent phases across GitHub issues | runner, coordinator |
| **3-phase loop** | A single orchestration iteration: plan → implement+review → merge | pipeline, workflow |
| **plan phase** | Phase where the planner agent analyzes open issues and produces an ordered plan | planning step |
| **implement phase** | Phase where implementer agents fix individual issues in isolated worktrees | coding step |
| **review phase** | Phase where the reviewer agent checks an implementer's changes before merge | code review step |
| **merge phase** | Phase where the merger agent integrates completed branches and closes issues | integration step |
| **issue** | A GitHub issue labeled for agent processing, representing one unit of work | ticket, task, card |
| **worktree** | An isolated git working tree created inside a container for a single issue | workspace, branch dir |

## Prompts

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **prompt** | A markdown file in the prompts directory that drives an agent's behavior for one phase | instruction, template |
| **prompts directory** | The `prompts/` subdirectory inside the pycastle directory holding all prompt files | templates dir |
| **placeholder** | A `{{VARIABLE}}` token inside a prompt, substituted at render time | template variable, slot |
| **shell expression** | A `` !`command` `` token inside a prompt, replaced by the command's stdout output at preprocess time | shell expansion |
| **prompt pipeline** | The two-stage process of rendering placeholders then preprocessing shell expressions | templating, rendering |
| **CODING_STANDARDS.md** | A reference document placed in the prompts directory and treated as a prompt for discovery and scaffolding purposes | standards file |

## Infrastructure

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Dockerfile** | File in the pycastle directory defining the Docker image for agent containers — ships without baked-in credentials | image definition |
| **container runner** | Package module that manages Docker container lifecycle and injects runtime secrets | docker wrapper |
| **runtime injection** | The act of reading `~/.claude.json` from the host and writing it to `/home/agent/.claude.json` inside a container before the agent runs | baking in, build-time config |

## Relationships

- A **consuming project** contains exactly one **pycastle directory**.
- A **pycastle directory** contains one **config.py**, one **.env**, one **Dockerfile**, and one **prompts directory**.
- A **prompts directory** contains one **prompt** per orchestration phase plus **CODING_STANDARDS.md**.
- The **orchestrator** runs one or more **3-phase loops**, bounded by `MAX_ITERATIONS`.
- Each **issue** must carry the **issue label** to be picked up by the **plan phase**.
- Each **issue** is processed by one **implementer agent** followed by one **reviewer agent** in the **implement phase**.
- The **container runner** performs **runtime injection** before every agent run.
- The **canonical label set** is defined once in the pycastle package; **pycastle labels** applies it to any target repo.
- A **label reset** deletes all existing repo labels before applying the **canonical label set**.

## Example dialogue

> **Dev:** "I just ran `pycastle init` on a new project — do I need to set credentials somewhere?"

> **Domain expert:** "The **init wizard** collects your **GH_TOKEN** and **CLAUDE_CODE_OAUTH_TOKEN** interactively and writes them into the **.env** in your **pycastle directory**. You still need to fill in `ANTHROPIC_API_KEY` manually if you're not using the OAuth token."

> **Dev:** "And the Claude account data — does that go in .env too?"

> **Domain expert:** "No. **CLAUDE_ACCOUNT_JSON** is read from `~/.claude.json` on your machine at runtime. The **container runner** performs **runtime injection** — it writes that file into each container before the **agent** starts. It never lives in **.env**."

> **Dev:** "What's the difference between `ready-for-agent` and the **canonical label set**?"

> **Domain expert:** "`ready-for-agent` is the **issue label** — the specific value the **orchestrator** filters on when picking up work. The **canonical label set** is the full set of seven labels that pycastle uses to manage issue state across a repo's lifecycle. Run `pycastle labels` to apply the whole set to a new repo."

> **Dev:** "If the repo already has labels, will `pycastle labels` overwrite them?"

> **Domain expert:** "By default it skips existing labels. Choose **label reset** to delete everything first and recreate from the **canonical label set** — useful when a repo inherited GitHub's default labels you don't use."

## Flagged ambiguities

- **"config"** is used loosely to mean either `config.py` (behavioral settings) or the combined configuration of a project (config.py + .env). Use **config.py** when referring to the file, and **pycastle directory** when referring to the full set of local overrides.
- **"prompt"** is used both for phase-driving instructions (plan-prompt.md, implement-prompt.md, etc.) and for `CODING_STANDARDS.md`, which is a reference document, not an instruction. The canonical rule: everything in the **prompts directory** is called a **prompt** for discovery and scaffolding purposes, regardless of whether it drives an agent phase directly.
- **"defaults"** can mean either (a) the bundled template files copied by `pycastle init`, or (b) the default values inside `config.py`. Prefer **defaults** for (a) and **default config values** for (b).
- **"token"** is overloaded: **GH_TOKEN** (GitHub PAT), **CLAUDE_CODE_OAUTH_TOKEN** (Claude subscription token), and **ANTHROPIC_API_KEY** (direct API key) are all called "tokens" in conversation. Always use the full env var name when precision matters.
- **"label"** can mean a GitHub label object (name + description + color) or the specific **issue label** value (`ready-for-agent`) that triggers agent processing. Use **label** for the former and **issue label** for the latter.
