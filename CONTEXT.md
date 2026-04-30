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
| **config.py** | Python file in the pycastle directory defining behavioral configuration; overrides the defaults module field by field at runtime | settings.py, settings |
| **defaults module** | `src/pycastle/defaults/config.py` bundled in the package; contains only pure default values, no logic; never touched by users or the config loader directly | defaults config, fallback config |
| **config loader** | The `loader.py` module inside the `config/` package; reads the defaults, executes the consuming project's config.py via importlib, and applies any programmatic overrides; contains no subprocess calls and no default values of its own | — |
| **config validator** | The `validator.py` module inside the `config/` package; owns `validate_config(cfg, claude_service) -> Config`; resolves model shorthands to full model IDs and validates effort levels; raises `ConfigValidationError` on any invalid entry; returns a new immutable `Config` via `dataclasses.replace` | — |
| **.env** | File in the pycastle directory holding secrets and credentials only — never committed to git | environment file, config |
| **GH_TOKEN** | GitHub personal access token stored in .env, used for GitHub API calls and label management | github token, gh pat |
| **CLAUDE_CODE_OAUTH_TOKEN** | Long-lived OAuth token for Claude Code authentication, stored in .env | claude token, oauth token |
| **ANTHROPIC_API_KEY** | Alternative Claude Code authentication via direct API key; not required when CLAUDE_CODE_OAUTH_TOKEN is set | api key, anthropic token |
| **CLAUDE_ACCOUNT_JSON** | Serialized Claude Code account credentials blob, read from `~/.claude.json` on the host at runtime — never stored in .env | claude config, claude json |
| **PREFLIGHT_CHECKS** | Config entry (`list[tuple[str, str]]`) of `(name, command)` pairs run during the Pre-flight phase; detection-only commands, no auto-fix (e.g. `ruff check .`) | preflight commands, check list |
| **IMPLEMENT_CHECKS** | Config entry (`list[str]`) of commands rendered into the FEEDBACK LOOPS section of the implement-prompt as agent instructions; may differ from PREFLIGHT_CHECKS (e.g. `ruff check --fix`) | feedback commands, implement commands |
| **field-by-field override** | The config loader strategy: for each non-underscore name in the consuming project's config.py, `setattr` replaces the corresponding name in the config loader module; absent names fall back to the defaults module | full replacement, merge override |
| **STAGE_OVERRIDES** | Config dict with one entry per orchestration phase (`plan`, `implement`, `review`, `merge`), each holding a model shorthand and an effort level | stage config, model config |
| **stage override** | The per-phase `model` + `effort` entry inside STAGE_OVERRIDES for one orchestration phase | phase config, agent config |
| **model shorthand** | A short family alias (`haiku`, `sonnet`, `opus`) that pycastle resolves to the latest full model ID at startup | model alias, model name |
| **full model ID** | The versioned Claude model identifier (e.g. `claude-sonnet-4-6`) resolved from a model shorthand via `claude list-models` | model ID, model version |
| **effort level** | One of three Claude effort values (`low`, `normal`, `high`) that controls cost and reasoning depth | effort, effort flag |
| **CLI default** | The behavior when no `--model` or `--effort` flag is injected — triggered by an empty string in STAGE_OVERRIDES | default model, unset |
| **validate_config** | Public function in the config validator module; takes a `Config` and a `ClaudeService`, resolves model shorthands to full model IDs, validates all stage overrides, and returns a new immutable `Config`; raises `ConfigValidationError` on any invalid entry | config validation, startup check |
| **ConfigValidationError** | Error raised by validate_config when a model shorthand or effort level is unrecognised; includes the invalid value, closest valid suggestion, and full list of valid options | validation error, config error |

## GitHub Integration

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **label** | A GitHub issue classification marker with a name, description, and hex color | tag, category |
| **canonical label set** | The six labels hardcoded in the pycastle package: `bug`, `needs-info`, `needs-triage`, `ready-for-agent`, `ready-for-human`, `wontfix` | default labels, label config |
| **label reset** | The option to delete all existing labels in a repo before creating the canonical label set | label wipe, clean labels |
| **issue label** | The specific label value (default: `ready-for-agent`) that marks a GitHub issue as eligible for agent processing | trigger label, agent label |

## Agents & Orchestration

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent** | A Claude Code instance running inside an isolated Docker container | bot, worker |
| **orchestrator** | The main loop that coordinates agent phases across GitHub issues | runner, coordinator |
| **iteration** | One complete plan-bounded loop ending after all planned issues have been attempted; up to MAX_ITERATIONS per `pycastle run` invocation; in parallel mode a single merge phase closes the iteration, in sequential mode each issue gets its own merge before the next starts | cycle, round, pipeline |
| **3-phase loop** | The structure of one iteration: plan phase, implement+review phase, merge phase | pipeline, workflow |
| **sequential mode** | The orchestration behavior that auto-activates when `max_parallel = 1`; within one iteration each issue is processed individually — implement → review → merge — with the safe SHA re-pinned from HEAD after each merge before the next issue starts; eliminates the merge conflicts that arise from parallel branches sharing the same base SHA | serial mode, one-at-a-time mode |
| **plan phase** | Phase where the Planner analyzes open issues and produces a plan | planning step |
| **implement phase** | Phase where Implementers fix individual issues in isolated worktrees | coding step |
| **review phase** | Phase where the Reviewer checks an Implementer's changes before merge | code review step |
| **merge phase** | Phase where completed branches are integrated into the main branch, issues are closed, and — when conflicts exist — the Merger is spawned | integration step |
| **Planner** | Agent role that runs during the plan phase; runs in a clean checkout of the safe SHA so it sees the same committed state that Implementers will start from; outputs a plan | planning agent |
| **Implementer** | Agent role that runs during the implement phase; one Implementer per issue | coding agent, implementation agent |
| **Reviewer** | Agent role that runs after an Implementer completes; validates changes before merge | review agent |
| **Merger** | Agent role spawned during the merge phase only when at least one conflicting branch exists; integrates only the conflicting branches and closes their issues | merge agent, integration agent |
| **preflight-issue agent** | Agent spawned when a quality check fails at the orchestrator level; explores the codebase to find root cause, evaluates whether HITL is required, files one structured GitHub issue, and outputs the issue number as `<issue>NUMBER</issue>`; always runs with skip_preflight enabled | bug-report agent, error reporter |
| **HITL verdict** | The routing decision encoded in the preflight issue's label after the preflight-issue agent completes; `ready-for-agent` means the orchestrator spawns a single Implementer, `ready-for-human` means the orchestrator aborts | HITL decision, routing verdict |
| **safe SHA** | The exact git commit SHA captured by the orchestrator after a passing preflight check; the Planner and all Implementer worktrees in the current iteration are created from this SHA, guaranteeing every agent sees the same verified-clean committed state | verified SHA, clean SHA |
| **cold startup** | The state at the beginning of a fresh `pycastle run` when no post-merge check has run yet in the current process; always triggers a pre-planning preflight check | first iteration, fresh start |
| **preflight-fix path** | The orchestrator routing when a preflight issue is AFK: skip the Planner, spawn one Implementer for the preflight issue, run the normal Reviewer → merge → post-merge check pipeline, then start a new iteration | preflight fast path |
| **programmatic merge path** | Fast-path logic in the merge phase that runs `git merge --no-edit` directly via subprocess without spawning the Merger; used when all branches merge cleanly | fast path, direct merge |
| **clean merge** | A `git merge --no-edit` that exits zero and requires no conflict resolution | conflict-free merge, successful merge |
| **conflicting branch** | A branch whose `git merge --no-edit` exits non-zero; `git merge --abort` is run immediately and the branch is collected for the Merger | failed merge branch |
| **post-merge check** | A quality check run on the host after all clean merges complete; uses the same PREFLIGHT_CHECKS commands as the Pre-flight phase; on pass, repins the safe SHA; on fail, spawns the preflight-issue agent | post-merge quality check, post-merge gate |
| **RALPH** | The required commit message prefix for all Implementer commits (e.g. `RALPH: fix auth bug`) | — |
| **plan** | The structured JSON output by the Planner listing which issues to work on and the branch name for each; after parsing, `plan_phase()` sorts issues by ascending issue number so the orchestrator always processes older issues first | plan output, plan JSON |
| **issue** | A GitHub issue labeled for agent processing, representing one unit of work | ticket, task, card |
| **AFK issue** | An issue the Planner assigns to an Implementer because it can be resolved autonomously; labeled `ready-for-agent` | agent issue, auto issue |
| **HITL issue** | An issue that requires human intervention; labeled `ready-for-human` — the Planner must never assign it to an Implementer | manual issue, human issue |
| **blocker** | An issue that must be resolved before another can be worked on | dependency, prerequisite |
| **dependency graph** | The set of blocker relationships between issues, analyzed by the Planner to determine the safe working set for an iteration | issue graph, dependency map |
| **worktree** | An isolated git working tree created on the host and bind-mounted into an agent container; either a named-branch worktree for an Implementer/Reviewer/Merger, a named-branch merge-sandbox worktree for the Merger, or a detached plan-sandbox worktree for the plan phase | workspace, branch dir |
| **plan-sandbox worktree** | A temporary detached checkout of the safe SHA created by `plan_phase` at the start of each iteration; shared by the Pre-flight phase, the preflight-issue agent (if spawned), and the Planner; always removed in a `try/finally` by `plan_phase` regardless of state; never associated with a branch; located at `.pycastle/.worktrees/plan-sandbox` | pre-planning worktree, planner worktree, temp worktree |
| **merge-sandbox worktree** | A temporary named-branch worktree (`pycastle/merge-sandbox`) created by `merge_phase` from HEAD after clean merges complete; the Merger runs inside it to resolve conflicting branches; always removed in a `try/finally` by `merge_phase` regardless of state; on success `merge_phase` fast-forwards `main` from the branch before cleanup; located at `.pycastle/.worktrees/merge-sandbox` | merger worktree, conflict worktree |
| **branch** | A git branch name assigned to an issue inside the plan; follows the pattern `pycastle/issue-<n>-<slug>` | feature branch, issue branch |
| **orphan worktree** | A worktree directory under `.pycastle/.worktrees/` no longer registered in git, typically left by a crashed agent run | stale worktree, leftover worktree |
| **orphan sweep** | Startup operation that cross-references `.pycastle/.worktrees/` against `git worktree list --porcelain` and deletes unregistered directories | worktree cleanup, stale cleanup |
| **collision detection** | Mechanism that prevents two parallel agents from simultaneously creating worktrees for the same branch, implemented as a per-branch async lock | — |

## Prompts

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **prompt** | A markdown file in the prompts directory that drives an agent's behavior for one phase | instruction, template |
| **prompts directory** | The `prompts/` subdirectory inside the pycastle directory holding all prompt files | templates dir |
| **placeholder** | A `{{VARIABLE}}` token inside a prompt, substituted at render time | template variable, slot |
| **shell expression** | A `` !`command` `` token inside a prompt, replaced by the command's stdout output at preprocess time | shell expansion |
| **prompt pipeline** | The two-stage process of rendering placeholders then preprocessing shell expressions | templating, rendering |
| **CODING_STANDARDS.md** | A reference document placed in the prompts directory and treated as a prompt for discovery and scaffolding purposes | standards file |
| **EXPLORATION section** | The section of the implement prompt that instructs the Implementer to read files before coding; scoped to files mentioned in the issue body — not a full repository survey | explore section, discovery section |
| **FEEDBACK LOOPS section** | The section of the implement prompt that instructs the Implementer to run IMPLEMENT_CHECKS commands before committing | feedback section, pre-commit checks |
| **`{{FEEDBACK_COMMANDS}}`** | Placeholder in the implement-prompt rendered at run time from `config.IMPLEMENT_CHECKS`; produces a backtick-formatted command list | — |
| **preflight-issue.md** | Prompt used by the preflight-issue agent; receives `{{CHECK_NAME}}`, `{{COMMAND}}`, and `{{OUTPUT}}` placeholders; explores the codebase to find root cause, evaluates HITL, writes a structured issue body, applies configured labels (never `needs-triage`), and outputs `<issue>NUMBER</issue>` | bug-report.md, error prompt |
| **`{{CHECKS}}`** | Placeholder in the merge-prompt rendered at run time from `config.PREFLIGHT_CHECKS`; injects quality check commands into the Merger agent's prompt | — |
| **Explore subagent** | A Claude Code subagent spawned by the Implementer during the EXPLORATION section to read relevant files; token usage bounded by scoping to the issue body | explore agent, repo scanner |

## Agent Output Protocol

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent output protocol** | The contract between prompts and the orchestrator: the set of XML tags agents emit to signal structured output (`<plan>`, `<issue>`, `<promise>`), plus the module that owns parsing and extraction of those tags | output format, agent tags, agent signals |
| **`<plan>` tag** | XML tag emitted by the Planner containing a JSON payload listing unblocked issues for the current iteration; extracted by the agent output protocol module | plan output, plan block |
| **`<issue>` tag** | XML tag emitted by the preflight-issue agent containing the GitHub issue number it filed; extracted by the agent output protocol module | issue output, issue number tag |
| **`<promise>COMPLETE</promise>`** | XML tag emitted by Implementers, Reviewers, and the Merger to declare that their work phase is complete; detected by the agent output protocol module | done signal, completion tag |
| **`AgentOutputProtocolError`** | Base exception raised by the agent output protocol module when a required tag is missing or malformed; subclassed by `PlanParseError`, `IssueParseError`, and `PromiseParseError` | parse error, protocol error |
| **`parse()`** | Entry point for data-bearing roles (Planner, preflight-issue agent); returns a typed output (`PlannerOutput` or `IssueOutput`) resolved statically by role; also checks for `<promise>COMPLETE</promise>` | protocol parser, output extractor |
| **`assert_complete()`** | Entry point for completion-only roles (Implementer, Reviewer, Merger); verifies the `<promise>COMPLETE</promise>` tag is present and raises `PromiseParseError` if not; returns `None` on success — never a bool | parse_completion, check_promise |
| **Claude streaming envelope** | The NDJSON format Claude Code uses for structured output; lines are JSON objects and the agent's final result is carried in the `{"type": "result", "result": "..."}` line; unwrapped internally by the agent output protocol module before tag extraction | streaming format, NDJSON output |

## Agent Lifecycle

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent lifecycle phase** | One of four named stages (Setup, Pre-flight, Prepare, Work) within a single agent container run | step, stage |
| **Setup phase** | First agent lifecycle phase: worktree creation, gitdir overlay creation, parent git dir mount wiring, container start, and git identity propagation | container setup, init phase |
| **Pre-flight phase** | Second agent lifecycle phase: runs quality checks sequentially inside the container and returns a list of failure tuples to the orchestrator; does not spawn agents internally | preflight, pre-flight check phase |
| **quality check** | One command run during the Pre-flight phase or a post-merge check, as defined in PREFLIGHT_CHECKS; each runs independently so all failures are collected in a single pass | quality gate, check |
| **check stage** | The lifecycle context prefix embedded in CHECK_NAME when a preflight-issue agent is spawned (e.g. `[plan-sandbox]`, `[post-merge]`); included in the filed GitHub issue title | stage prefix, phase prefix |
| **pre-flight failure** | Result of a quality check returning non-zero during the Pre-flight phase; returned as a failure tuple to the orchestrator | check failure |
| **post-merge failure** | Result of a quality check returning non-zero during the post-merge check; triggers the preflight-issue agent with check stage `[post-merge]`; the Merger is not spawned | post-merge check failure |
| **pre-existing failure** | A pre-flight failure that existed before the current agent's task began; root cause of scope creep | baseline failure |
| **scope creep** | The behavior where an agent modifies files outside its assigned task scope, typically caused by inheriting pre-existing failures | overreach |
| **skip_preflight** | Flag on `run_agent()` that bypasses the Pre-flight phase; always True for the preflight-issue agent; defaults to False for all other agents | — |
| **Prepare phase** | Third agent lifecycle phase: dependency installation, prompt rendering, and prompt injection into the container | hook phase, pre-work |
| **Work phase** | Fourth agent lifecycle phase: Claude Code invocation and streaming output collection | execution phase, run phase |
| **git identity propagation** | Setup phase operation that reads the host `git user.name` and `git user.email` and configures them inside the container | git config injection, user setup |
| **idle timeout** | Maximum wall-clock seconds an agent may produce no output before being killed and raising AgentTimeoutError; default 300 s | inactivity timeout, silence timeout |
| **worktree timeout** | Maximum wall-clock seconds a git worktree operation may take before raising WorktreeTimeoutError; default 30 s | git timeout |
| **errors log** | Append-only `logs/errors.log` recording full tracebacks for every failed agent run, separated by timestamped dividers | error file, crash log |

## Infrastructure

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Dockerfile** | File in the pycastle directory defining the Docker image for agent containers — ships without baked-in credentials | image definition |
| **container runner** | Package module that manages Docker container lifecycle and injects runtime secrets | docker wrapper |
| **host repo** | The git repository on the developer's machine that is mounted into each agent container | project repo, local repo |
| **volume mount** | A Docker bind mount attaching a host filesystem path to a container-internal path, with an explicit read/write mode | bind mount, volume |
| **RO mount** | A volume mount with `mode: "ro"` — the container cannot write to it; used for the host repo | read-only mount |
| **RW mount** | A volume mount with `mode: "rw"` — the container can read and write; used for the worktree and parent git dir mount | read-write mount |
| **gitdir file** | The `.git` file inside a git worktree directory; contains a `gitdir:` pointer to the parent repo's worktree metadata directory | .git file, git pointer |
| **gitdir overlay** | A host temp file containing a corrected `gitdir:` path, mounted over the worktree's gitdir file inside the container so Linux git resolves the parent repo path correctly; needed only on Windows hosts | git file patch, gitdir patch |
| **parent git dir mount** | A RW mount that binds `<host-repo>/.git` to `/.pycastle-parent-git` inside the container, giving the agent write access to worktree metadata without making the rest of the host repo writable | git dir mount, .git mount |
| **`/.pycastle-parent-git`** | The deterministic container-internal path where the parent git dir mount is bound; referenced by the gitdir overlay | — |
| **worktree setup** | Container initialization step that runs `git worktree add` on the host to create the worktree from the safe SHA before the agent prompt is sent | worktree init, worktree creation |
| **new-branch path** | The `git worktree add -b <branch> <path> <safe-SHA>` form used when the branch does not yet exist; always branched from the pinned safe SHA rather than HEAD | — |
| **existing-branch path** | The `git worktree add <path> <branch>` form used when the branch already exists | — |
| **worktree contents check** | Guard step run after `git worktree add` that verifies `pyproject.toml` or `requirements.txt` is present; fails with the worktree path and directory listing if absent | checkout guard, file check |
| **runtime injection** | The act of reading `~/.claude.json` from the host and writing it to `/home/agent/.claude.json` inside a container before the agent runs | baking in, build-time config |
| **PycastleError** | Base exception class for all pycastle domain errors | — |
| **DockerError** | Error raised when a Docker operation (container start, stop, remove) fails | container error |
| **WorktreeError** | Error raised when a git worktree operation fails for a non-timeout reason | git error |
| **WorktreeTimeoutError** | Error raised when a git worktree operation exceeds the worktree timeout | — |
| **AgentTimeoutError** | Error raised when an agent produces no output for longer than the idle timeout | hung agent error |

## Service Abstraction & Dependency Injection

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Service** | An injectable abstraction that encapsulates all subprocess calls for a single external tool or domain (Git, Claude CLI, Docker) | Provider, adapter, wrapper |
| **service interface** | The public methods of a Service that callers depend on; never exposes subprocess calls or tool-specific details | Contract, API |
| **Custom exception hierarchy** | Domain-specific exception types raised by a Service (e.g. `GitCommandError`, `GitTimeoutError`); callers never see raw subprocess exceptions | Tool exceptions, system errors |
| **Dependency injection** | Pattern of passing Service implementations to functions/classes that depend on them, enabling tests to inject mocks | Parameter injection, constructor injection |
| **test fixture** | A pytest fixture that provides Default implementations for all Services; individual tests override for specific scenarios | Mock factory, test helper |
| **Default implementation** | A Service implementation provided by a test fixture that returns deterministic values instead of making real subprocess calls | Mock, test double |
| **GitService** | Service that encapsulates all git subprocess operations (config, worktree management, branch queries, remote info, programmatic merges); worktree creation accepts an optional safe SHA | Git wrapper, git provider |
| **ClaudeService** | Service that encapsulates the `claude list-models` subprocess call with process-lifetime caching | Claude wrapper, model provider |
| **DockerService** | Service that encapsulates the `docker build` subprocess call with support for build args | Docker wrapper, build provider |
| **GithubService** | Service that encapsulates `gh` CLI calls for GitHub issue operations: closing issues, querying parent issues, listing open sub-issues, and reading issue labels | GitHub wrapper, gh provider |
| **Logger** | Injectable abstraction that owns all structured log output for one iteration; exposes named channels (`log_error`, `log_agent_output`) each writing to a dedicated file under `logs/`; injected via `Deps` so tests never touch the filesystem | log writer, output handler |
| **RecordingLogger** | Test double for `Logger` that records every call in memory; tests assert on recorded calls rather than capturing stderr or reading log files | mock logger, spy logger |
| **StatusDisplay** | Injectable abstraction that owns the live terminal status line; exposes `add_agent`, `update_phase`, `remove_agent`, and `print` methods; backed by a `rich` `Live` display in production and a `NullStatusDisplay` no-op in tests; injected via `Deps` as a separate concern from `Logger` | terminal display, status bar |
| **NullStatusDisplay** | Test/no-op implementation of `StatusDisplay`; all methods are no-ops; `print` falls back to `builtins.print` | — |
| **agent status row** | One row in the `StatusDisplay` live panel representing one active agent; shows agent name, current agent lifecycle phase, idle seconds since last raw chunk, and an OSC 8 clickable link to the agent's log file; ordered by orchestration phase (plan → implement → review → merge) then by issue number; disappears when the agent finishes | status entry, agent row |
| **OSC 8 link** | Terminal hyperlink using the OSC 8 ANSI escape sequence (`\033]8;;file:///path\033\\text\033]8;;\033\\`); used in the agent status row to make the log file path clickable; link text is the bare file path | terminal link, clickable link |
| **IterationOutcome** | Sealed return type of `run_iteration()`; one of four variants: `Continue` (iteration completed, keep looping), `Done` (no issues found, stop cleanly), `AbortedHITL` (HITL verdict — carries `issue_number`; orchestrator exits non-zero), `AbortedUsageLimit` (token ceiling hit — worktrees preserved, safe to retry; orchestrator exits non-zero) | iteration result, loop result |

## Test Anti-Patterns (Red Flags)

| Term | Definition | Why it's a problem |
| --- | --- | --- |
| **Verifying through external means** | Tests that call subprocess, query databases, or check external state directly instead of testing through the service interface | Requires external tools in test environment; failures reflect environment problems, not code bugs |
| **Mocking internal collaborators** | Tests that mock classes or functions within the same codebase rather than using dependency injection | Creates brittle tests coupled to implementation; breaks on refactoring |
| **Testing private methods** | Tests that call functions prefixed with `_` or access private attributes | Private methods are implementation details; test public behavior instead |
| **Asserting on call counts/order** | Tests that verify internal function calls happened N times or in a specific sequence | Breaks on refactoring; couples tests to implementation rather than behavior |
| **Test name describes HOW not WHAT** | Test names like `test_calls_git_config` instead of `test_setup_configures_git_identity` | Developers cannot understand test intent from the name alone |

## Relationships

- **STAGE_OVERRIDES** has exactly four entries, one per orchestration phase (`plan`, `implement`, `review`, `merge`); each entry has independent `model` and `effort` fields — an empty string for either means CLI default (no flag injected).
- **validate_config** runs once at `orchestrator.run()` start; queries `claude list-models` once per process (cached); after completion, all non-empty `model` entries in STAGE_OVERRIDES contain full model IDs, not shorthands.
- The **Planner** produces one plan per iteration listing only unblocked AFK issues; blockers and HITL issues are excluded via the dependency graph.
- Each AFK issue in a plan is processed by exactly one **Implementer** followed by one **Reviewer**.
- The **merge phase** attempts the programmatic merge path for every branch sequentially; the **Merger** is spawned at most once per iteration and only when conflicting branches exist.
- A **post-merge check** runs on the host if any clean merges occurred; on pass, the **safe SHA** is repinned to the new HEAD; on fail, the **preflight-issue agent** is spawned with check stage `[post-merge]` and the Merger is not spawned.
- A **pre-planning preflight** runs at **cold startup** and is skipped when the previous iteration ended with a passing post-merge check; on pass, the **safe SHA** is pinned to current HEAD. The preflight runs inside a **plan-sandbox worktree** — a detached checkout of HEAD — so it sees only committed files, never live host state or active Implementer worktrees.
- The **preflight-issue agent** is spawned at most once per preflight failure session, acting on the first failing check by PREFLIGHT_CHECKS order; always runs with skip_preflight to prevent circular failures; mounts the same **plan-sandbox worktree** as the Planner so it explores the same committed state where the failure occurred.
- The **HITL verdict** is read by the orchestrator from the GitHub issue label after the **preflight-issue agent** completes; `ready-for-agent` triggers the **preflight-fix path**, `ready-for-human` aborts with a non-zero exit code.
- On the **preflight-fix path**, the Planner is skipped; one Implementer is spawned for the preflight issue, followed by one Reviewer, then a merge and post-merge check; a new iteration then begins from the post-merge check result.
- The **Planner** and all **Implementer** worktrees are created from the pinned **safe SHA**, never from HEAD directly; this guarantees every agent sees the same verified-clean committed state regardless of external commits that land on main after preflight passes.
- In **sequential mode** (`max_parallel = 1`), the iteration processes issues one by one: after each issue's merge the safe SHA is re-pinned to the new HEAD, and the next Implementer starts from that SHA; a failed issue is skipped (remains `ready-for-agent`) and the queue continues; the Merger remains available as a fallback for unexpected conflicts; no additional pre-flight checks run between issues.
- The **Pre-flight phase** (agent lifecycle) runs quality checks inside the container and returns a list of failure tuples to the orchestrator; it never spawns agents internally.
- An **orphan sweep** runs once at orchestrator startup; **collision detection** holds a per-branch lock for the full duration of each agent run.
- Host mounts per container: host repo → RO at `/home/agent/repo`; worktree → RW at `/home/agent/workspace`; `<host-repo>/.git` → RW at `/.pycastle-parent-git`; on Windows, gitdir overlay → RO over `/home/agent/workspace/.git`.
- A **Service** defines a Custom exception hierarchy so callers never handle raw subprocess exceptions; tests inject Default implementations from a test fixture and override per-test for error paths.
- **StatusDisplay** is a separate injectable in `Deps` alongside `Logger`; `Logger` owns file I/O, `StatusDisplay` owns the live terminal UI — they never overlap.
- The **agent status row** is created when the Setup phase begins (log file is also created at this point so the OSC 8 link is always valid); the row is removed when the agent finishes or errors; the `rich` `Live` display is started on the first `add_agent` call and stopped after the last `remove_agent` call.
- All orchestrator-level terminal output (e.g. "Planning complete…") is routed through `StatusDisplay.print()` so `rich` can coordinate it with the live panel; bare `print()` calls are not used while a `StatusDisplay` is active.
- Streaming agent messages are no longer printed to the terminal; they are still consumed from the Docker output stream (to reset the idle timeout) and written to the per-agent log file, but `_format_stream_line` output is suppressed; the status row is the sole terminal surface for active agents.

## Example dialogue

> **Dev:** "If ruff, mypy, and pytest all fail at startup, do we file three issues?"

> **Domain expert:** "No — we pick the first failure by PREFLIGHT_CHECKS order and file exactly one issue via the **preflight-issue agent**. The agent explores the codebase, determines root cause, and decides the **HITL verdict**. The other failures surface in the next iteration's preflight."

> **Dev:** "What if the agent isn't sure whether a human is needed?"

> **Domain expert:** "It defaults to `ready-for-human`. The **HITL verdict** is read directly from the issue label — that label is the single source of truth. If it's `ready-for-human`, we return **PlanHITL** and exit. The operator goes to GitHub to see the filed issue."

> **Dev:** "And if it's `ready-for-agent`, how does the Implementer know it's starting from a clean state?"

> **Domain expert:** "The **safe SHA** was pinned when preflight passed. The Implementer's worktree is always created from that SHA — never from HEAD. So even if something lands on main between preflight and the Implementer spinning up, the agent starts from the verified-clean commit."

> **Dev:** "After the preflight fix merges, do we re-run preflight before planning the next iteration?"

> **Domain expert:** "No. The **post-merge check** repins the **safe SHA** on pass. That's the signal the next iteration uses — we don't run preflight twice back-to-back. We're in **cold startup** only on the very first iteration of a fresh run."

## Flagged ambiguities

- **"preflight"** appears in two distinct contexts: the **Pre-flight phase** (an agent lifecycle phase that runs inside a container and returns failure tuples) and the orchestrator-level **preflight check** (which the orchestrator runs before planning and after merges). These are related but distinct — the Pre-flight phase is the mechanism, the orchestrator-level check is the policy that decides when to run it and what to do with failures.
- **"bug-report agent"** and **"bug-report.md"** are removed by the preflight refactor and replaced by **preflight-issue agent** and **preflight-issue.md**. Any reference to the bug-report agent in existing code, tests, or documentation refers to the old behavior.
