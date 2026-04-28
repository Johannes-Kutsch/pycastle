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
| **config loader** | `src/pycastle/config.py`; imports everything from the defaults module, then applies field-by-field overrides from the consuming project's config.py if it exists; contains no default values of its own | — |
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
| **validate_config** | Startup function that resolves model shorthands to full model IDs and validates all stage overrides, mutating STAGE_OVERRIDES in-memory; raises ConfigValidationError on any invalid entry | config validation, startup check |
| **ConfigValidationError** | Error raised by validate_config when a model shorthand or effort level is unrecognised; includes the invalid value, closest valid suggestion, and full list of valid options | validation error, config error |

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
| **iteration** | One complete 3-phase loop (plan → implement+review → merge); up to MAX_ITERATIONS per `pycastle run` invocation | cycle, round, pipeline |
| **3-phase loop** | The structure of one iteration: plan phase, implement+review phase, merge phase | pipeline, workflow |
| **plan phase** | Phase where the Planner analyzes open issues and produces a plan | planning step |
| **implement phase** | Phase where Implementers fix individual issues in isolated worktrees | coding step |
| **review phase** | Phase where the Reviewer checks an Implementer's changes before merge | code review step |
| **merge phase** | Phase where completed branches are integrated into the main branch, issues are closed, and — when conflicts exist — the Merger is spawned | integration step |
| **Planner** | Agent role that runs during the plan phase; outputs a plan | planning agent |
| **Implementer** | Agent role that runs during the implement phase; one Implementer per issue | coding agent, implementation agent |
| **Reviewer** | Agent role that runs after an Implementer completes; validates changes before merge | review agent |
| **Merger** | Agent role spawned during the merge phase only when at least one conflicting branch exists; integrates only the conflicting branches and closes their issues | merge agent, integration agent |
| **programmatic merge path** | Fast-path logic in the merge phase that runs `git merge --no-edit` directly via subprocess without spawning the Merger; used when all branches merge cleanly | fast path, direct merge |
| **clean merge** | A `git merge --no-edit` that exits zero and requires no conflict resolution | conflict-free merge, successful merge |
| **conflicting branch** | A branch whose `git merge --no-edit` exits non-zero; `git merge --abort` is run immediately and the branch is collected for the Merger | failed merge branch |
| **post-merge check** | A quality check run on the host after all clean merges complete; uses the same PREFLIGHT_CHECKS commands as the Pre-flight phase | post-merge quality check, post-merge gate |
| **bug-report agent** | On-demand agent spawned when a quality check fails; files one GitHub issue per failure with the check stage in the title; always runs with skip_preflight enabled | error reporter, bug filer |
| **RALPH** | The required commit message prefix for all Implementer commits (e.g. `RALPH: fix auth bug`) | — |
| **plan** | The structured JSON output by the Planner listing which issues to work on and the branch name for each | plan output, plan JSON |
| **issue** | A GitHub issue labeled for agent processing, representing one unit of work | ticket, task, card |
| **AFK issue** | An issue the Planner assigns to an Implementer because it can be resolved autonomously; labeled `ready-for-agent` | agent issue, auto issue |
| **HITL issue** | An issue that requires human intervention; labeled `ready-for-human` — the Planner must never assign it to an Implementer | manual issue, human issue |
| **blocker** | An issue that must be resolved before another can be worked on | dependency, prerequisite |
| **dependency graph** | The set of blocker relationships between issues, analyzed by the Planner to determine the safe working set for an iteration | issue graph, dependency map |
| **worktree** | An isolated git working tree created on the host for a single issue and bind-mounted into the agent container | workspace, branch dir |
| **branch** | A git branch name assigned to an issue inside the plan; follows the pattern `sandcastle/issue-<n>-<slug>` | feature branch, issue branch |
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
| **bug-report.md** | Prompt used by the bug-report agent; receives `{{CHECK_NAME}}`, `{{COMMAND}}`, and `{{OUTPUT}}` placeholders; creates one GitHub issue titled `{{CHECK_NAME}} failed` with `bug` and `needs-triage` labels | error prompt, preflight prompt |
| **`{{CHECKS}}`** | Placeholder in the merge-prompt rendered at run time from `config.PREFLIGHT_CHECKS`; injects quality check commands into the Merger agent's prompt | — |
| **Explore subagent** | A Claude Code subagent spawned by the Implementer during the EXPLORATION section to read relevant files; token usage bounded by scoping to the issue body | explore agent, repo scanner |

## Agent Lifecycle

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent lifecycle phase** | One of four named stages (Setup, Pre-flight, Prepare, Work) within a single agent container run | step, stage |
| **Setup phase** | First agent lifecycle phase: worktree creation, gitdir overlay creation, parent git dir mount wiring, container start, and git identity propagation | container setup, init phase |
| **Pre-flight phase** | Second agent lifecycle phase: runs quality checks sequentially inside the container; on any failure spawns a bug-report agent per failing check then raises PreflightError | preflight, pre-flight check phase |
| **quality check** | One command run during the Pre-flight phase or a post-merge check, as defined in PREFLIGHT_CHECKS; each runs independently so all failures are reported in a single pass | quality gate, check |
| **check stage** | The lifecycle context prefix embedded in CHECK_NAME when a bug-report agent is spawned (e.g. `[pre-planning]`, `[post-merge]`); included in the filed GitHub issue title | stage prefix, phase prefix |
| **pre-flight failure** | Result of a quality check returning non-zero during the Pre-flight phase | check failure |
| **post-merge failure** | Result of a quality check returning non-zero during the post-merge check; triggers a bug-report agent with check stage `[post-merge]`; the Merger is not spawned | post-merge check failure |
| **pre-existing failure** | A pre-flight failure that existed before the current agent's task began; root cause of scope creep | baseline failure |
| **scope creep** | The behavior where an agent modifies files outside its assigned task scope, typically caused by inheriting pre-existing failures | overreach |
| **skip_preflight** | Flag on `run_agent()` that bypasses the Pre-flight phase; always True for the bug-report agent; defaults to False for all other agents | — |
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
| **worktree setup** | Container initialization step that runs `git worktree add` on the host to create the worktree before the agent prompt is sent | worktree init, worktree creation |
| **new-branch path** | The `git worktree add -b <branch> <path> HEAD` form used when the branch does not yet exist; `HEAD` must be passed explicitly on Windows Docker mounts | — |
| **existing-branch path** | The `git worktree add <path> <branch>` form used when the branch already exists | — |
| **worktree contents check** | Guard step run after `git worktree add` that verifies `pyproject.toml` or `requirements.txt` is present; fails with the worktree path and directory listing if absent | checkout guard, file check |
| **runtime injection** | The act of reading `~/.claude.json` from the host and writing it to `/home/agent/.claude.json` inside a container before the agent runs | baking in, build-time config |
| **PycastleError** | Base exception class for all pycastle domain errors | — |
| **DockerError** | Error raised when a Docker operation (container start, stop, remove) fails | container error |
| **WorktreeError** | Error raised when a git worktree operation fails for a non-timeout reason | git error |
| **WorktreeTimeoutError** | Error raised when a git worktree operation exceeds the worktree timeout | — |
| **AgentTimeoutError** | Error raised when an agent produces no output for longer than the idle timeout | hung agent error |
| **PreflightError** | Error raised by `run_agent()` after all bug-report agents have been spawned for pre-flight failures; signals callers to abort | preflight error |

## Service Abstraction & Dependency Injection

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Service** | An injectable abstraction that encapsulates all subprocess calls for a single external tool or domain (Git, Claude CLI, Docker) | Provider, adapter, wrapper |
| **service interface** | The public methods of a Service that callers depend on; never exposes subprocess calls or tool-specific details | Contract, API |
| **Custom exception hierarchy** | Domain-specific exception types raised by a Service (e.g. `GitCommandError`, `GitTimeoutError`); callers never see raw subprocess exceptions | Tool exceptions, system errors |
| **Dependency injection** | Pattern of passing Service implementations to functions/classes that depend on them, enabling tests to inject mocks | Parameter injection, constructor injection |
| **test fixture** | A pytest fixture that provides Default implementations for all Services; individual tests override for specific scenarios | Mock factory, test helper |
| **Default implementation** | A Service implementation provided by a test fixture that returns deterministic values instead of making real subprocess calls | Mock, test double |
| **GitService** | Service that encapsulates all git subprocess operations (config, worktree management, branch queries, remote info, programmatic merges) | Git wrapper, git provider |
| **ClaudeService** | Service that encapsulates the `claude list-models` subprocess call with process-lifetime caching | Claude wrapper, model provider |
| **DockerService** | Service that encapsulates the `docker build` subprocess call with support for build args | Docker wrapper, build provider |
| **GithubService** | Service that encapsulates `gh` CLI calls for GitHub issue operations: closing issues, querying parent issues, and listing open sub-issues | GitHub wrapper, gh provider |

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
- A **post-merge check** runs on the host if any clean merges occurred; failure spawns a bug-report agent with check stage `[post-merge]` but does not spawn the Merger.
- A **pre-flight failure** in the Planner's container raises PreflightError and aborts the entire orchestrator run; in an Implementer's container it raises PreflightError and skips only that issue.
- The **bug-report agent** is spawned once per failing quality check (not once per run); always runs with skip_preflight to prevent circular failures.
- An **orphan sweep** runs once at orchestrator startup; **collision detection** holds a per-branch lock for the full duration of each agent run.
- Host mounts per container: host repo → RO at `/home/agent/repo`; worktree → RW at `/home/agent/workspace`; `<host-repo>/.git` → RW at `/.pycastle-parent-git`; on Windows, gitdir overlay → RO over `/home/agent/workspace/.git`.
- A **Service** defines a Custom exception hierarchy so callers never handle raw subprocess exceptions; tests inject Default implementations from a test fixture and override per-test for error paths.
