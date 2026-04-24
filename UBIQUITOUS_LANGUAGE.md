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
| **iteration** | One complete 3-phase loop (plan → implement+review → merge); up to `MAX_ITERATIONS` run per `pycastle run` invocation | cycle, round, pipeline |
| **3-phase loop** | The structure of a single **iteration**: plan phase, implement+review phase, merge phase | pipeline, workflow |
| **plan phase** | Phase where the **Planner** analyzes open issues and produces a **plan** | planning step |
| **implement phase** | Phase where **Implementers** fix individual issues in isolated **worktrees** | coding step |
| **review phase** | Phase where the **Reviewer** checks an **Implementer**'s changes before merge | code review step |
| **merge phase** | Phase where the **Merger** integrates completed branches and closes issues | integration step |
| **Planner** | The named agent role that runs during the **plan phase**; outputs a **plan** | planning agent |
| **Implementer** | The named agent role that runs during the **implement phase**; one **Implementer** per **issue** | coding agent, implementation agent |
| **Reviewer** | The named agent role that runs after an **Implementer** completes; validates changes before merge | review agent |
| **Merger** | The named agent role that runs during the **merge phase**; integrates all completed branches | merge agent, integration agent |
| **plan** | The structured data (JSON) output by the **Planner** listing which issues to work on and the branch name for each | plan output, plan JSON |
| **issue** | A GitHub issue labeled for agent processing, representing one unit of work | ticket, task, card |
| **AFK issue** | An issue the **Planner** assigns to an **Implementer** because it can be resolved autonomously; labeled `ready-for-agent` | agent issue, auto issue |
| **HITL issue** | An issue that requires human intervention; labeled `ready-for-human` — the **Planner** must never assign it to an **Implementer** | manual issue, human issue |
| **blocker** | An issue that must be resolved before another issue can be worked on; informs the **Planner**'s selection | dependency, prerequisite |
| **dependency graph** | The set of blocker relationships between issues, analyzed by the **Planner** to determine the safe working set for an **iteration** | issue graph, dependency map |
| **worktree** | An isolated git working tree created on the **host** for a single issue and bind-mounted into the agent container | workspace, branch dir |
| **branch** | A git branch name assigned to an **issue** inside the **plan**; follows the pattern `sandcastle/issue-<n>-<slug>` | feature branch, issue branch |
| **orphan worktree** | A worktree directory under `.pycastle/.worktrees/` that is no longer registered in git, typically left by a crashed agent run | stale worktree, leftover worktree |
| **orphan sweep** | The startup operation that cross-references `.pycastle/.worktrees/` against `git worktree list --porcelain` and deletes any unregistered directories | worktree cleanup, stale cleanup |
| **collision detection** | The mechanism that prevents two parallel agents from simultaneously creating worktrees for the same branch, implemented as a per-branch async lock | — |

## Prompts

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **prompt** | A markdown file in the prompts directory that drives an agent's behavior for one phase | instruction, template |
| **prompts directory** | The `prompts/` subdirectory inside the pycastle directory holding all prompt files | templates dir |
| **placeholder** | A `{{VARIABLE}}` token inside a prompt, substituted at render time | template variable, slot |
| **shell expression** | A `` !`command` `` token inside a prompt, replaced by the command's stdout output at preprocess time | shell expansion |
| **prompt pipeline** | The two-stage process of rendering placeholders then preprocessing shell expressions | templating, rendering |
| **CODING_STANDARDS.md** | A reference document placed in the prompts directory and treated as a prompt for discovery and scaffolding purposes | standards file |

## Agent Lifecycle

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent lifecycle phase** | One of three named stages (Setup, Prepare, Work) within a single agent container run | step, stage |
| **Setup phase** | The first agent lifecycle phase: worktree creation, gitdir patching, container start, and git identity propagation | container setup, init phase |
| **Prepare phase** | The second agent lifecycle phase: dependency installation, prompt rendering, and prompt injection into the container | hook phase, pre-work |
| **Work phase** | The third agent lifecycle phase: Claude Code invocation and streaming output collection | execution phase, run phase |
| **git identity propagation** | The Setup phase operation that reads the host `git user.name` and `git user.email` and configures them inside the container so that `git commit` succeeds | git config injection, user setup |
| **idle timeout** | The maximum wall-clock seconds an agent may produce no output before being killed and raising `AgentTimeoutError`; default 300 s | inactivity timeout, silence timeout |
| **worktree timeout** | The maximum wall-clock seconds a git worktree operation may take before being killed and raising `WorktreeTimeoutError`; default 30 s | git timeout |
| **errors log** | The append-only `logs/errors.log` file that records full tracebacks for every failed agent run, separated by timestamped dividers | error file, crash log |

## Infrastructure

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Dockerfile** | File in the pycastle directory defining the Docker image for agent containers — ships without baked-in credentials | image definition |
| **container runner** | Package module that manages Docker container lifecycle and injects runtime secrets | docker wrapper |
| **host repo** | The git repository on the developer's machine that is mounted read-write into each agent container | project repo, local repo |
| **volume mount** | The Docker bind mount that attaches the **host repo** at `/home/agent/repo` inside each container | bind mount, volume |
| **worktree setup** | The container initialization step that runs `git worktree add` to create the **worktree** for an **Implementer** before the agent prompt is sent; uses the new-branch path when the branch doesn't exist, the existing-branch path when it does | worktree init, worktree creation |
| **new-branch path** | The `git worktree add -b <branch> <path> HEAD` form used when the **branch** does not yet exist; `HEAD` must be passed explicitly to force commit resolution on Windows Docker mounts | — |
| **existing-branch path** | The `git worktree add <path> <branch>` form used when the **branch** already exists; the branch name serves as the commit-ish | — |
| **worktree contents check** | The guard step run after `git worktree add` that verifies `pyproject.toml` or `requirements.txt` is present in the **worktree**; fails with the worktree path and directory listing if absent | checkout guard, file check |
| **runtime injection** | The act of reading `~/.claude.json` from the host and writing it to `/home/agent/.claude.json` inside a container before the agent runs | baking in, build-time config |
| **PycastleError** | Base exception class for all pycastle domain errors; all agent, container, and worktree failures subclass it | — |
| **DockerError** | Error subclass raised when a Docker operation (container start, stop, remove) fails | container error |
| **WorktreeError** | Error subclass raised when a git worktree operation fails for a non-timeout reason | git error |
| **WorktreeTimeoutError** | Error subclass raised when a git worktree operation exceeds the **worktree timeout** | — |
| **AgentTimeoutError** | Error subclass raised when an agent produces no output for longer than the **idle timeout** | hung agent error |

## Relationships

- A **consuming project** contains exactly one **pycastle directory**.
- A **pycastle directory** contains one **config.py**, one **.env**, one **Dockerfile**, and one **prompts directory**.
- A **prompts directory** contains one **prompt** per orchestration phase plus **CODING_STANDARDS.md**.
- The **orchestrator** runs one or more **iterations**, bounded by `MAX_ITERATIONS`.
- Each **iteration** consists of one **plan phase**, one **implement phase** (with embedded **review phase**), and one **merge phase**.
- The **Planner** runs once per **iteration** and produces exactly one **plan**.
- A **plan** contains one entry per unblocked **AFK issue**, each paired with a **branch** name.
- The **Planner** uses the **dependency graph** to exclude **blockers** and **HITL issues** from the **plan**.
- Each **AFK issue** in a **plan** is processed by one **Implementer** followed by one **Reviewer**.
- Each **Implementer** runs inside its own container with its own **worktree**, created by **worktree setup** before the agent prompt is sent.
- The **Merger** runs once per **iteration** after all **Implementers** and **Reviewers** complete.
- Each **issue** must carry the **issue label** to be picked up by the **Planner**.
- An **AFK issue** carries `ready-for-agent`; a **HITL issue** carries `ready-for-human`.
- The **container runner** performs **runtime injection** before every agent run.
- **Worktree setup** always runs the **worktree contents check** immediately after `git worktree add`; a failed check raises an error with the worktree path and directory listing.
- **Worktree setup** uses the **new-branch path** when the branch doesn't yet exist, and the **existing-branch path** when it does.
- The **host repo** is attached to each container via a **volume mount** at `/home/agent/repo`.
- The **canonical label set** is defined once in the pycastle package; **pycastle labels** applies it to any target repo.
- A **label reset** deletes all existing repo labels before applying the **canonical label set**.
- Each agent run progresses through three **agent lifecycle phases** in order: **Setup phase** → **Prepare phase** → **Work phase**.
- The **Setup phase** always includes **git identity propagation** before the agent prompt is sent.
- An **orphan sweep** runs once at **orchestrator** startup (not per agent) to avoid racing with active worktrees.
- **Collision detection** holds a per-branch lock for the full duration of an agent run, from **worktree setup** through worktree removal.
- A `WorktreeTimeoutError` is raised when any git operation within **worktree setup** exceeds the **worktree timeout**.
- An `AgentTimeoutError` is raised when the **Work phase** produces no output for longer than the **idle timeout**.
- All failed agent runs append a full traceback to the **errors log**, separated by a timestamped divider.

## Example dialogue

> **Dev:** "I just ran `pycastle init` on a new project — do I need to set credentials somewhere?"

> **Domain expert:** "The **init wizard** collects your **GH_TOKEN** and **CLAUDE_CODE_OAUTH_TOKEN** interactively and writes them into the **.env** in your **pycastle directory**. You still need to fill in `ANTHROPIC_API_KEY` manually if you're not using the OAuth token."

> **Dev:** "And the Claude account data — does that go in .env too?"

> **Domain expert:** "No. **CLAUDE_ACCOUNT_JSON** is read from `~/.claude.json` on your machine at runtime. The **container runner** performs **runtime injection** — it writes that file into each container before the **agent** starts. It never lives in **.env**."

> **Dev:** "When I run `pycastle run`, the **Planner** listed three issues but only two showed up in the **plan**. Why?"

> **Domain expert:** "The **Planner** reads the **dependency graph**. If an issue has an open **blocker**, it's excluded from the **plan** for that **iteration**. The blocked issue will appear in a future **iteration** once its **blocker** is resolved and merged."

> **Dev:** "What if one of the issues is labeled `ready-for-human`?"

> **Domain expert:** "That's a **HITL issue** — the **Planner** must never assign it to an **Implementer**. Only **AFK issues** (labeled `ready-for-agent`) go into the **plan**. A **HITL issue** needs a human to act on it directly."

> **Dev:** "The **Implementer** for issue #4 failed with 'no pyproject.toml found in worktree /home/agent/workspace-sandcastle-issue-4-...' The file is definitely committed."

> **Domain expert:** "That's a **worktree contents check** failure. The **container runner** runs **worktree setup**, which calls `git worktree add` via the **volume mount**. On Windows Docker mounts, git may create the directory but skip the file checkout unless `HEAD` is passed explicitly — that's the **new-branch path**. The error now includes the worktree path and a directory listing so you can see exactly what git did check out."

## Example dialogue (extended)

> **Dev:** "The implementer for issue #7 timed out — what does that mean exactly?"

> **Domain expert:** "There are two kinds of timeout. A **worktree timeout** fires if `git worktree add` takes more than 30 seconds during the **Setup phase** — that raises `WorktreeTimeoutError`. An **idle timeout** fires if the **Work phase** produces no output for 300 seconds — that raises `AgentTimeoutError`. Check the **errors log** to see which one it was and at what timestamp."

> **Dev:** "Could two agents collide on the same branch?"

> **Domain expert:** "Not anymore. **Collision detection** holds a per-branch lock for the entire agent run. The second agent will wait until the first completes worktree removal before it can start **worktree setup**. If the **Planner** somehow assigned the same branch twice, the second agent fails fast with a clear error rather than corrupting the worktree."

> **Dev:** "After a crash I see leftover directories under `.pycastle/.worktrees/`. Will they cause problems?"

> **Domain expert:** "Those are **orphan worktrees**. The **orphan sweep** runs at the start of every `pycastle run` — it compares those directories against `git worktree list --porcelain` and deletes anything git no longer knows about. By the time the first **iteration** starts, the slate is clean."

## Flagged ambiguities

- **"config"** is used loosely to mean either `config.py` (behavioral settings) or the combined configuration of a project (config.py + .env). Use **config.py** when referring to the file, and **pycastle directory** when referring to the full set of local overrides.
- **"prompt"** is used both for phase-driving instructions (plan-prompt.md, implement-prompt.md, etc.) and for `CODING_STANDARDS.md`, which is a reference document, not an instruction. The canonical rule: everything in the **prompts directory** is called a **prompt** for discovery and scaffolding purposes, regardless of whether it drives an agent phase directly.
- **"defaults"** can mean either (a) the bundled template files copied by `pycastle init`, or (b) the default values inside `config.py`. Prefer **defaults** for (a) and **default config values** for (b).
- **"token"** is overloaded: **GH_TOKEN** (GitHub PAT), **CLAUDE_CODE_OAUTH_TOKEN** (Claude subscription token), and **ANTHROPIC_API_KEY** (direct API key) are all called "tokens" in conversation. Always use the full env var name when precision matters.
- **"label"** can mean a GitHub label object (name + description + color) or the specific **issue label** value (`ready-for-agent`) that triggers agent processing. Use **label** for the former and **issue label** for the latter.
- **"plan"** is used to mean both (a) the act of planning (the **plan phase**) and (b) the structured artifact the **Planner** produces (a JSON list of issue/branch pairs). Use **plan phase** for the former and **plan** for the latter.
- **"agent"** sometimes refers to a specific named role (Planner, Implementer, Reviewer, Merger) and sometimes to any Claude Code container instance. Use the specific role name when precision matters; use **agent** only when referring to the concept generically.
- **"AFK"** and **"HITL"** are not surfaced in the pycastle UI or label names — they are workflow concepts. Their concrete representation is the **issue label**: `ready-for-agent` for **AFK issues**, `ready-for-human` for **HITL issues**. Never conflate the concept with the label name.
- **"phase"** now operates at two levels: *orchestration phases* (plan, implement, review, merge) are stages of an **iteration**; *agent lifecycle phases* (Setup, Prepare, Work) are stages of a single agent container run. Use the full term (**agent lifecycle phase** vs **plan phase**) when the level is not obvious from context.
- **"worktree"** was previously defined as "created inside a container" — this is incorrect. The **worktree** is always created on the **host** and bind-mounted into the container. The container never runs `git worktree add`.
- **"timeout"** is used for two distinct limits: **idle timeout** (agent produces no output) and **worktree timeout** (git operation takes too long). Always qualify which kind is meant.
