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
| **config loader** | The `loader.py` module inside the `config/` package; reads the defaults, executes the consuming project's config.py via importlib, applies any programmatic overrides, validates effort strings, and returns an immutable `Config`; pure — no subprocess calls, no service dependencies, no default values of its own | — |
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
| **model shorthand** | A short family alias (`haiku`, `sonnet`, `opus`) accepted by the Claude CLI natively; stored as-is in `Config` and passed through to `claude --model` at stage execution time; not resolved at config load time (see ADR 0002) | model alias, model name |
| **full model ID** | The versioned Claude model identifier (e.g. `claude-sonnet-4-6`); may be stored directly in a stage override instead of a shorthand; passed through to `claude --model` unchanged | model ID, model version |
| **effort level** | One of five Claude effort values (`low`, `medium`, `high`, `xhigh`, `max`) that controls cost and reasoning depth; validated at config load time against this fixed set | effort, effort flag |
| **CLI default** | The behavior when no `--model` or `--effort` flag is injected — triggered by an empty string in STAGE_OVERRIDES | default model, unset |
| **ConfigValidationError** | Error raised by the config loader when an effort level is unrecognised; includes the invalid value, closest valid suggestion, and full list of valid options | validation error, config error |
| **auto_push** | Boolean config entry (default `True`) that controls whether `merge_phase` pushes local main to the remote after any merges produce commits; set to `False` to disable automatic pushing | push_after_merge, AUTO_PUSH |

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
| **safe SHA** | The exact git commit SHA captured by the orchestrator after a `git pull --ff-only` and a passing preflight check; the Planner and all Implementer worktrees in the current iteration are created from this SHA, guaranteeing every agent sees the same verified-clean committed state reflecting the latest remote state | verified SHA, clean SHA |
| **preflight pull** | A `git pull --ff-only` run at the start of every preflight phase, before the safe SHA is pinned; waits for the working tree to be clean first; aborts with an error if the remote has diverged or is unreachable | — |
| **preflight-fix path** | The orchestrator routing when a preflight issue is AFK: skip the Planner, spawn one Implementer for the preflight issue, run the normal Reviewer → merge pipeline, then start a new iteration | preflight fast path |
| **programmatic merge path** | Fast-path logic in the merge phase that runs `git merge --no-edit` directly via subprocess without spawning the Merger; used when all branches merge cleanly | fast path, direct merge |
| **clean merge** | A `git merge --no-edit` that exits zero and requires no conflict resolution | conflict-free merge, successful merge |
| **conflicting branch** | A branch whose `git merge --no-edit` exits non-zero; `git merge --abort` is run immediately and the branch is collected for the Merger | failed merge branch |
| **RALPH: Implement -** | The commit message prefix injected by `run_issue()` in code for all Implementer commits (e.g. `RALPH: Implement - fix auth bug`); prepended to the message the Implementer outputs inside `<commit_message>` tags; used by the implement skip to detect whether implement work is complete | — |
| **RALPH: Review -** | The commit message prefix injected by `run_issue()` in code for all Reviewer commits (e.g. `RALPH: Review - improve error handling`); prepended to the message the Reviewer outputs inside `<commit_message>` tags; used by the review skip to detect whether review work is complete; each agent produces exactly one commit per branch | — |
| **`<commit_message>` tag** | XML tag emitted by Implementers and Reviewers instead of `<promise>COMPLETE</promise>`; contains the agent's plain description of changes (no prefix); the orchestrator prepends the appropriate RALPH prefix, stages all worktree changes with `git add -A`, and commits; absence of this tag is a failed run — worktree is preserved and the agent restarts to continue | — |
| **in-flight issue** | An open issue that has an existing `pycastle/issue-<n>` branch or worktree from a previous interrupted iteration; signals that implement or review work is already partially or fully complete | mid-flight issue, resumed issue |
| **merge-time preflight skip** | The behavior when the Merger's Pre-flight phase returns failures: `merge_phase` logs a diagnostic, skips the Merger, and returns normally with conflict issues still pending; the next iteration's pre-planning preflight detects the broken baseline and recovers via the preflight-fix path | merge preflight abort |
| **planning skip** | The behavior in `run_iteration` when at least one open issue is in-flight: the Planner is not invoked and only the in-flight issues are used as the working set for the current iteration; issues with neither a branch nor a worktree are deferred | plan bypass |
| **implement skip** | The behavior in `run_issue` when a branch already has a `RALPH:` (non-review) commit: the Implementer is not spawned and the Reviewer runs directly via the existing-branch path | — |
| **review skip** | The behavior in `run_issue` when a branch already has a `RALPH: Review -` commit: both the Implementer and Reviewer are skipped and the issue is counted as completed immediately | — |
| **plan** | The structured JSON output by the Planner listing which issues to work on and the branch name for each; after parsing, `plan_phase()` sorts issues by ascending issue number so the orchestrator always processes older issues first | plan output, plan JSON |
| **issue** | A GitHub issue labeled for agent processing, representing one unit of work | ticket, task, card |
| **AFK issue** | An issue the Planner assigns to an Implementer because it can be resolved autonomously; labeled `ready-for-agent` | agent issue, auto issue |
| **HITL issue** | An issue that requires human intervention; labeled `ready-for-human` — the Planner must never assign it to an Implementer | manual issue, human issue |
| **blocker** | An issue that must be resolved before another can be worked on | dependency, prerequisite |
| **dependency graph** | The set of blocker relationships between issues, analyzed by the Planner to determine the safe working set for an iteration | issue graph, dependency map |
| **worktree** | An isolated git working tree created on the host and bind-mounted into an agent container; either a named-branch worktree for an Implementer/Reviewer/Merger, a named-branch merge-sandbox worktree for the Merger, a detached pre-flight-sandbox worktree for the Pre-flight phase, or a detached plan-sandbox worktree for the planning phase | workspace, branch dir |
| **pre-flight-sandbox worktree** | A temporary detached checkout of the safe SHA created by `preflight_phase` at the start of each iteration; shared by the Pre-flight phase and the preflight-issue agent (if spawned); always removed in a `try/finally` by `preflight_phase` regardless of state; never associated with a branch; located at `.pycastle/.worktrees/pre-flight-sandbox` | pre-planning worktree, temp worktree |
| **plan-sandbox worktree** | A temporary detached checkout of the safe SHA created by `planning_phase` when the Planner is invoked; always removed in a `try/finally` by `planning_phase` regardless of state; never associated with a branch; located at `.pycastle/.worktrees/plan-sandbox` | planner worktree |
| **merge-sandbox worktree** | A temporary named-branch worktree (`pycastle/merge-sandbox`) created by `merge_phase` from HEAD after clean merges complete; the Merger runs inside it to resolve conflicting branches; always removed in a `try/finally` by `merge_phase` regardless of state; on success `merge_phase` fast-forwards `main` from the branch before cleanup; located at `.pycastle/.worktrees/merge-sandbox` | merger worktree, conflict worktree |
| **branch** | A git branch name assigned to an issue inside the plan; follows the pattern `pycastle/issue-<n>-<slug>` | feature branch, issue branch |
| **orphan worktree** | A worktree directory under `.pycastle/.worktrees/` no longer registered in git, typically left by a crashed agent run | stale worktree, leftover worktree |
| **orphan sweep** | Startup operation that cross-references `.pycastle/.worktrees/` against `git worktree list --porcelain`, deletes unregistered directories, and removes the `.worktrees` parent directory if no active children remain | worktree cleanup, stale cleanup |
| **collision detection** | Mechanism that prevents two parallel agents from simultaneously creating worktrees for the same branch, implemented as a per-branch async lock | — |

## Prompts

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **prompt** | A markdown file in the prompts directory that drives an agent's behavior for one phase | instruction, template |
| **prompts directory** | The `prompts/` subdirectory inside the pycastle directory holding all prompt files | templates dir |
| **placeholder** | A `{{VARIABLE}}` token inside a prompt, substituted at render time | template variable, slot |
| **shell expression** | A `` !`command` `` token inside a prompt, replaced by the command's stdout output at preprocess time | shell expansion |
| **prompt pipeline** | The single module (`prompt_pipeline`) owning all prompt concerns: loading coding-standard files from the prompts directory (`load_standards`), rendering `{{placeholders}}` against an args dict, and preprocessing `` !`shell` `` expressions; exposes `prepare_prompt`, `load_standards`, and `PromptRenderError` | templating, rendering |
| **`load_standards`** | Function in `prompt_pipeline` that reads the five coding-standard files from the `coding-standards/` subdirectory of the prompts directory and returns a `dict[str, str]` keyed by placeholder name (`TESTING_STANDARDS`, `MOCKING_STANDARDS`, `INTERFACES_STANDARDS`, `DEEP_MODULES_STANDARDS`, `REFACTORING_STANDARDS`); missing files return an empty string | — |
| **CODING_STANDARDS.md** | A reference document placed in the prompts directory and treated as a prompt for discovery and scaffolding purposes | standards file |
| **EXPLORATION section** | The section of the implement prompt that instructs the Implementer to read files before coding; scoped to files mentioned in the issue body — not a full repository survey | explore section, discovery section |
| **FEEDBACK LOOPS section** | The section of the implement prompt that instructs the Implementer to run IMPLEMENT_CHECKS commands before committing | feedback section, pre-commit checks |
| **`{{FEEDBACK_COMMANDS}}`** | Placeholder in the implement-prompt rendered at run time from `config.IMPLEMENT_CHECKS`; produces a backtick-formatted command list | — |
| **preflight-issue.md** | Prompt used by the preflight-issue agent; receives `{{CHECK_NAME}}`, `{{COMMAND}}`, `{{OUTPUT}}`, `{{BUG_LABEL}}`, `{{ISSUE_LABEL}}`, and `{{HITL_LABEL}}` placeholders; explores the codebase to find root cause, evaluates HITL, writes a structured issue body, applies configured labels (never `needs-triage`), and outputs `<issue>NUMBER</issue>` | bug-report.md, error prompt |
| **`{{CHECKS}}`** | Placeholder in the merge-prompt rendered at run time from `config.PREFLIGHT_CHECKS`; injects quality check commands into the Merger agent's prompt | — |
| **Explore subagent** | A Claude Code subagent spawned by the Implementer during the EXPLORATION section to read relevant files; token usage bounded by scoping to the issue body | explore agent, repo scanner |

## Agent Output Protocol

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent output protocol** | The contract between prompts and the orchestrator: the set of XML tags agents emit to signal structured output (`<plan>`, `<issue>`, `<commit_message>`, `<promise>`), plus the module that owns the complete NDJSON stream → typed output pipeline | output format, agent tags, agent signals |
| **`<plan>` tag** | XML tag emitted by the Planner containing a JSON payload listing unblocked issues for the current iteration; extracted by the agent output protocol module | plan output, plan block |
| **`<issue>` tag** | XML tag emitted by the preflight-issue agent containing the GitHub issue number it filed; extracted by the agent output protocol module | issue output, issue number tag |
| **`<promise>COMPLETE</promise>`** | XML tag emitted by the Merger and the preflight-issue agent to declare that their work phase is complete; Implementers and Reviewers use `<commit_message>` instead | done signal, completion tag |
| **`AgentOutputProtocolError`** | Base exception raised by the agent output protocol module when a required tag is missing or malformed; subclassed by `PlanParseError`, `IssueParseError`, `PromiseParseError`, and `CommitMessageParseError` | parse error, protocol error |
| **`CommitMessageParseError`** | Subclass of `AgentOutputProtocolError` raised when an Implementer or Reviewer completes without emitting a `<commit_message>` tag; treated as a failed run — worktree is preserved and the agent is restarted | — |
| **`CommitMessageOutput`** | Typed output returned by `process_stream` for IMPLEMENTER and REVIEWER roles; carries the agent's plain `message: str`; the orchestrator prepends the RALPH prefix before committing | — |
| **`process_stream()`** | Single entry point in the agent output protocol module; accepts an iterable of decoded NDJSON lines, an `on_turn` callback, and an `AgentRole`; drives the per-line loop, emits complete assistant turns via the callback, raises `UsageLimitError` immediately on detection of a 429 error response, unwraps the result envelope, and returns a typed `AgentOutput`; the container runner is the only caller — phases never call it directly | protocol entry point, stream processor |
| **`on_turn` callback** | A `Callable[[str], None]` passed to `process_stream` by the container runner; invoked once per complete assistant turn during the Work phase; constructed by the container runner as a lambda over `StatusDisplay.print` so the agent output protocol module has no dependency on `StatusDisplay` | turn callback, display hook |
| **Claude streaming envelope** | The NDJSON format Claude Code uses for structured output; lines are JSON objects and the agent's final result is carried in the `{"type": "result", "result": "..."}` line; unwrapped internally by `process_stream` before tag extraction | streaming format, NDJSON output |

## Agent Lifecycle

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **agent lifecycle phase** | One of three named stages (Setup, Pre-flight, Work) within a single agent container run; the Prepare phase was retired as a distinct stage — prompt rendering is now an internal step of the Work phase | step, stage |
| **Setup phase** | First agent lifecycle phase: worktree creation, gitdir overlay creation, parent git dir mount wiring, container start, git identity propagation, and consuming project dependency installation (`pip install -e '.[dev]'` or `pip install -r requirements.txt`); any tool referenced in PREFLIGHT_CHECKS must be declared in the consuming project's dependency file — the image does not provide dev tools as a fallback | container setup, init phase |
| **Pre-flight phase** | Second agent lifecycle phase: runs quality checks sequentially inside the container and returns a list of failure tuples to the orchestrator; does not spawn agents internally | preflight, pre-flight check phase |
| **quality check** | One command run during the Pre-flight phase, as defined in PREFLIGHT_CHECKS; each runs independently so all failures are collected in a single pass | quality gate, check |
| **check stage** | The lifecycle context prefix embedded in CHECK_NAME when a preflight-issue agent is spawned (e.g. `[PREFLIGHT]`); included in the filed GitHub issue title | stage prefix, phase prefix |
| **pre-flight failure** | Result of a quality check returning non-zero during the Pre-flight phase; returned as a failure tuple to the orchestrator | check failure |
| **pre-existing failure** | A pre-flight failure that existed before the current agent's task began; root cause of scope creep | baseline failure |
| **scope creep** | The behavior where an agent modifies files outside its assigned task scope, typically caused by inheriting pre-existing failures | overreach |
| **skip_preflight** | Flag on `run_agent()` that bypasses the Pre-flight phase; always True for the preflight-issue agent; defaults to False for all other agents | — |
| **Work phase** | Third agent lifecycle phase: prompt rendering and injection into the container, followed by Claude Code invocation and streaming output collection; prompt preparation is an internal step of `ContainerRunner.work()` — not a separate phase or method call | execution phase, run phase |
| **git identity propagation** | Setup phase operation that reads the host `git user.name` and `git user.email` and configures them inside the container | git config injection, user setup |
| **idle timeout** | Maximum wall-clock seconds an agent may produce no output before being killed and raising AgentTimeoutError; default 300 s | inactivity timeout, silence timeout |
| **worktree timeout** | Maximum wall-clock seconds a git worktree operation may take before raising WorktreeTimeoutError; default 30 s | git timeout |
| **errors log** | Append-only `logs/errors.log` recording full tracebacks for every failed agent run, separated by timestamped dividers | error file, crash log |

## Infrastructure

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Dockerfile** | File in the pycastle directory defining the Docker image for agent containers — ships without baked-in credentials and without baked-in dev tools; system utilities (git, gh), Claude Code CLI, and the Python runtime are the only baked-in contents; all dev tools (e.g. ruff, mypy, pytest) must be declared in the consuming project's dependency file and are installed at runtime during the Setup phase | image definition |
| **DockerSession** | Module in `docker_session.py` that owns Docker container lifecycle and low-level I/O; constructed from a pre-computed volume spec, filtered container environment, image name, config, and an optional `auto_overlay` path to delete on exit; exposes `exec_simple(command, timeout) → str` and `write_file(content, container_path)`; used by `ContainerRunner` as its Docker substrate — no agent-protocol concepts live here | docker client, container manager |
| **`build_volume_spec`** | Pure-ish function in `docker_session.py` that computes the complete Docker volume specification for a container run from host paths; owns the necessary file I/O: reads the `.git` file to locate the parent git dir, creates the gitdir overlay on Windows when needed; returns `(volumes_dict, auto_overlay)` where `auto_overlay` is a host path `DockerSession.__exit__` must delete, or `None` if no overlay was created | volume builder, mount spec |
| **container runner** | Package module that drives the three agent lifecycle phases (Setup, Pre-flight, Work) inside a `DockerSession`; constructed with a name, a `DockerSession` instance, model, effort, `status_display`, and config; delegates all Docker I/O to the session; during the Work phase renders the prompt, writes it to the container, then drives `WorkStream` for byte chunking, log writing, idle timeout detection, and delegates the line stream to `process_stream` | docker wrapper |
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
| **`detached_worktree`** | Async context manager in `worktree.py` that creates a detached checkout at a given SHA, yields the path, and guarantees removal in `__aexit__` regardless of outcome; also removes the `.worktrees` parent directory if no other worktrees remain after cleanup; used by `planning_phase` and `preflight_phase` for their sandbox worktrees | managed_worktree |
| **`branch_worktree`** | Async context manager in `worktree.py` that creates a named-branch worktree at a given SHA, yields the path, and on exit removes the worktree, optionally deletes the branch, and removes the `.worktrees` parent directory if no other worktrees remain; used by `merge_phase` for the merge-sandbox worktree | managed_worktree |
| **`_agent_worktree`** | Async context manager in `implement.py` that owns the full Implementer and Reviewer worktree lifecycle; accepts a branch name, SHA, `CancellationToken`, and `Deps`; on entry creates the worktree and gitdir overlay; on exit conditionally removes the worktree based on `token.wants_worktree_preserved` and working-tree cleanliness, and always removes the gitdir overlay; used by `run_issue` twice per issue — once for the Implementer (new-branch path) and once for the Reviewer (existing-branch path); defined in `implement.py` not `worktree.py` because its cleanup policy depends on agent-lifecycle state (`CancellationToken`) rather than being unconditional | managed_worktree |
| **`worktree_name_for_branch`** | Function in `worktree.py` that derives a short directory name from a branch string: extracts `issue-N` from `pycastle/issue-N-slug` or falls back to a sanitised slug; single authoritative definition replacing duplicated regex in `agent_runner` and `merge_phase` | — |
| **`worktree_path`** | Function in `worktree.py` that constructs the host filesystem path for a named worktree at `<repo_root>/<pycastle_dir>/.worktrees/<name>`; single authoritative path expression replacing duplication across all phase modules | — |
| **runtime injection** | The act of reading `~/.claude.json` from the host and writing it to `/home/agent/.claude.json` inside a container before the agent runs | baking in, build-time config |
| **WorkStream** | Class in `stream_session.py` that converts a raw Docker byte stream into an `AgentOutput`; constructed with a byte-chunk iterator, a log path, an idle timeout, and an `on_chunk: Callable[[], None]` callback; its `run(role, on_turn) → AgentOutput` method drives a feeder thread, writes each byte chunk to the log file (flushed immediately), calls `on_chunk()` per chunk, detects idle timeouts (raising `AgentTimeoutError`), splits bytes into complete UTF-8 lines, and delegates to `process_stream`; the only caller is `ContainerRunner.run_streaming`, which passes `status_display.reset_idle_timer` as the `on_chunk` callback | stream session, work session |
| **StreamParser** | Retired — its assistant-turn assembly logic is now a private implementation detail of `process_stream` in the agent output protocol module; `stream_parser.py` no longer exists as a public module | stream processor, message parser |
| **agent message** | The text content emitted by an agent during a single assistant turn; excludes tool-use and tool-result blocks; during the Work phase, printed to the console prefixed with the agent name and followed by a blank line; not shown in the status panel | assistant message, agent output |
| **PycastleError** | Base exception class for all pycastle domain errors | — |
| **DockerError** | Error raised when a Docker operation (container start, stop, remove) fails | container error |
| **WorktreeError** | Error raised when a git worktree operation fails for a non-timeout reason | git error |
| **WorktreeTimeoutError** | Error raised when a git worktree operation exceeds the worktree timeout | — |
| **AgentTimeoutError** | Error raised when an agent produces no output for longer than the idle timeout | hung agent error |

## Service Abstraction & Dependency Injection

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Service** | An injectable abstraction that encapsulates all subprocess calls for a single external tool or domain (Git, Claude CLI, Docker); all four Service modules live in `src/pycastle/services/` and are imported via `pycastle.services` | Provider, adapter, wrapper |
| **service interface** | The public methods of a Service that callers depend on; never exposes subprocess calls or tool-specific details | Contract, API |
| **Custom exception hierarchy** | Domain-specific exception types raised by a Service (e.g. `GitCommandError`, `GitTimeoutError`); callers never see raw subprocess exceptions | Tool exceptions, system errors |
| **`_SubprocessService`** | Private base class in `services/_base.py` inherited by `GitService` and `GithubService`; owns `_run` (exception translation), `_run_or_raise` (returncode check + domain error), and `_decode` (UTF-8 bytes → stripped str); not part of the public `pycastle.services` API | — |
| **Dependency injection** | Pattern of passing Service implementations to functions/classes that depend on them, enabling tests to inject mocks | Parameter injection, constructor injection |
| **test fixture** | A pytest fixture that provides Default implementations for all Services; individual tests override for specific scenarios | Mock factory, test helper |
| **Default implementation** | A Service implementation provided by a test fixture that returns deterministic values instead of making real subprocess calls | Mock, test double |
| **GitService** | Service that encapsulates all git subprocess operations (config, worktree management, branch queries, remote info, programmatic merges); worktree creation accepts an optional safe SHA | Git wrapper, git provider |
| **ClaudeService** | Service that encapsulates the `claude list-models` subprocess call with process-lifetime caching | Claude wrapper, model provider |
| **DockerService** | Service that encapsulates the `docker build` subprocess call with support for build args | Docker wrapper, build provider |
| **GithubService** | Service that encapsulates `gh` CLI calls for GitHub issue operations: closing issues, querying parent issues, listing open sub-issues, and reading issue labels | GitHub wrapper, gh provider |
| **`Deps`** | Concrete dataclass constructed once per iteration in the orchestrator and passed to `run_iteration`; bundles the full set of iteration-layer dependencies: `repo_root`, `git_svc`, `github_svc`, `agent_runner`, `cfg`, `logger`, and `status_display`; satisfies every per-phase dependency protocol via structural typing so the orchestrator passes it unmodified; `env` is intentionally absent — it is consumed at `AgentRunner` construction time before `Deps` is built and is not threaded through the iteration layer | iteration context, deps container |
| **per-phase dependency protocol** | A private `Protocol` class declared in each phase module listing only the fields that phase actually accesses; `Deps` satisfies every protocol via structural typing; tests construct minimal inline dataclasses with only the required fields instead of building a full `Deps`; follows the `_WorktreeDeps` pattern established in `worktree.py`; individual protocols: `_PreflightDeps` (in `preflight.py`), `_PlanningDeps` (in `planning.py`), `_ImplementDeps` (in `implement.py`), `_MergeDeps` (in `merge.py`), `_UtilDeps` (in `_utils.py`) | deps narrowing, phase context |
| **`_WorktreeDeps`** | Private protocol in `worktree.py` listing only the fields that worktree utilities need (`repo_root`, `cfg`, `git_svc`); the original instance of the per-phase dependency protocol pattern; satisfied by `Deps` structurally | — |
| **Logger** | Injectable abstraction that owns all structured log output for one iteration; exposes named channels (`log_error`, `log_agent_output`) each writing to a dedicated file under `logs/`; injected via `Deps` so tests never touch the filesystem | log writer, output handler |
| **RecordingLogger** | Test double for `Logger` that records every call in memory; tests assert on recorded calls rather than capturing stderr or reading log files | mock logger, spy logger |
| **StatusDisplay** | Injectable abstraction that owns the live terminal status panel and all formatted terminal output; exposes `register(caller, startup_message="started", work_body="", initial_phase="Setup")`, `update_phase`, `reset_idle_timer`, `remove(caller, shutdown_message="finished", shutdown_style="success")`, and `print(caller, message, style=None)` methods; `shutdown_message` and `message` may contain `\n` — each line is emitted separately with the `[Caller]` prefix and the same style applied to every line; `shutdown_style` accepts `"success"` (green), `"error"` (red), or `"warning"` (yellow); backed by a `rich` `Live` display in production and a `PlainStatusDisplay` in tests; injected via `Deps` as a separate concern from `Logger`; defined in `status_display` module | terminal display, status bar |
| **caller** | The identity string passed as the first argument to `StatusDisplay.register`, `remove`, and `print`; rendered as a `[Caller]` prefix on every terminal output line; empty string `""` is the anonymous caller — no brackets are printed and the message is output as-is; a blank line is inserted before any output call (`register`, `remove`, or `print`) when the caller differs from the previous one, unconditionally when the caller is `""` (anonymous outputs always stand alone), or before the very first output call (when no previous call has occurred); canonical callers — phase rows: `"Preflight"`, `"Plan"`, `"Implement"`, `"Merge"`; agents: `"Preflight Agent"`, `"Plan Agent"`, `"Implement Agent #N"`, `"Review Agent #N"`, `"Merge Agent"` | source, label |
| **work_body** | The caller-constructed string passed as the third argument to `register`; applies to agent rows only; displayed in the body column during the Work phase; empty string for agent rows that do not reach Work; unused by phase rows (which use `initial_phase` for their fixed body label) | — |
| **PlainStatusDisplay** | Plain-terminal adapter for `StatusDisplay` defined in `status_display` module; panel methods (`update_phase`, `reset_idle_timer`) are no-ops; `register` and `remove` print their startup/shutdown messages; `print(caller, message, style=None)` formats output as `[Caller] message` with no ANSI colour codes, no bold, and style ignored; multi-line messages are split and each line prefixed with `[Caller]`; used in tests so assertions can match the full formatted line | NullStatusDisplay |
| **phase_row** | Async context manager in `iteration/` that owns the `StatusDisplay` register/remove lifecycle for a single phase row; accepts `startup_message: str = "started"` forwarded to `register`; on entry calls `register(caller, startup_message=startup_message, initial_phase=initial_phase)`; yields a `PhaseRow` whose `close(shutdown_message, shutdown_style="success")` method calls `remove()` and marks the row as closed; if `close()` is never called before exit (exception path), automatically calls `remove(caller, "failed", shutdown_style="error")`; the canonical way to manage phase row lifecycle — replaces hand-rolled active-flag patterns | — |
| **status row** | One headerless line in the `StatusDisplay` live panel; created by `register` and removed by `remove`; two kinds: **agent rows** (one per active agent — `"Preflight Agent"`, `"Plan Agent"`, `"Implement Agent #N"`, `"Review Agent #N"`, `"Merge Agent"`) and **phase rows** (one per active phase — `"Preflight"`, `"Plan"`, `"Implement"`, `"Merge"`); phase rows and agent rows within the same phase coexist; format: `elapsed \| Name \| idle \| body`; elapsed is dim and right-justified; name is bold with any numeric part styled bold cyan; idle is dim; body column: for **agent rows**, shows the current agent lifecycle phase name for all non-Work states, or `work_body` during Work; for **phase rows**, shows a body derived from the phase: `"Planning"` for Plan, `"Merging"` for Merge, `"Running"` for Preflight; for the **Implement phase row** specifically, the body is dynamic — `"Running: started Agents for X/Y issues"` where Y is the total issue count for the phase and X increments each time an agent acquires the concurrency semaphore (monotonic; either an Implement Agent or Review Agent counts); elapsed counts up from `register` and never resets; idle resets on each Docker stream chunk; the live panel is preceded by one blank line to visually separate it from scrollback; ordered by orchestration phase (plan → implement → review → merge) then by issue number | agent status row, status entry, agent row |
| **IterationOutcome** | Sealed return type of `run_iteration()`; one of four variants: `Continue` (iteration completed, keep looping), `Done` (no issues found, stop cleanly), `AbortedHITL` (HITL verdict — carries `issue_number`; orchestrator exits non-zero), `AbortedUsageLimit` (token ceiling hit — carries `reset_time: datetime | None`; worktrees preserved; orchestrator sleeps until `reset_time + 2 min` when parsed from the Claude message, or until 2 minutes past the next local-time full hour when the reset time cannot be parsed; status message appends `"(estimated)"` on the fallback path; continues the loop to retry the current issue from scratch; repeats indefinitely on consecutive hits) | iteration result, loop result |

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
- **`load_config()`** is a pure function — no subprocess calls; it validates effort strings against the fixed set and raises `ConfigValidationError` on any invalid entry; model strings (shorthands or full IDs) are stored as-is in `Config` and resolved by the Claude CLI at stage execution time (see ADR 0002).
- The **Planner** produces one plan per iteration listing only unblocked AFK issues; blockers and HITL issues are excluded via the dependency graph.
- Each AFK issue in a plan is processed by exactly one **Implementer** followed by one **Reviewer**.
- The **merge phase** attempts the programmatic merge path for every branch sequentially; the **Merger** is spawned at most once per iteration and only when conflicting branches exist.
- A **pre-planning preflight** runs at the start of every iteration; it first performs a **preflight pull** to sync with the remote, then pins the **safe SHA** to the resulting HEAD. The preflight runs inside a **pre-flight-sandbox worktree** — a detached checkout of HEAD — so it sees only committed files, never live host state or active Implementer worktrees.
- The **preflight-issue agent** is spawned at most once per preflight failure session, acting on the first failing check by PREFLIGHT_CHECKS order; always runs with skip_preflight to prevent circular failures; mounts the same **pre-flight-sandbox worktree** used by the Pre-flight phase so it explores the same committed state where the failure occurred.
- The **HITL verdict** is read by the orchestrator from the GitHub issue label after the **preflight-issue agent** completes; `ready-for-agent` triggers the **preflight-fix path**, `ready-for-human` aborts with a non-zero exit code.
- On the **preflight-fix path**, the Planner is skipped; one Implementer is spawned for the preflight issue, followed by one Reviewer, then a merge; a new iteration then begins.
- The **Planner** and all **Implementer** worktrees are created from the pinned **safe SHA**, never from HEAD directly; this guarantees every agent sees the same verified-clean committed state regardless of external commits that land on main after preflight passes.
- The **planning skip** is checked before every Planner invocation; it takes priority over normal planning when any open issue is **in-flight**. The **implement skip** and **review skip** are checked inside `run_issue` before any worktree is created; they are mutually exclusive with normal agent spawning for that phase. Both skips are triggered by commit prefix detection (`RALPH: Review -` → review skip; `RALPH: Implement -` → implement skip only).
- A **merge-time preflight skip** leaves conflict issues open; they become **in-flight issues** on the next iteration, triggering the **planning skip** and then the **implement skip** or **review skip** as appropriate once the baseline is fixed.
- In **sequential mode** (`max_parallel = 1`), the iteration processes issues one by one: after each issue's merge the safe SHA is re-pinned to the new HEAD, and the next Implementer starts from that SHA; a failed issue is skipped (remains `ready-for-agent`) and the queue continues; the Merger remains available as a fallback for unexpected conflicts; no additional pre-flight checks run between issues.
- The **Pre-flight phase** (agent lifecycle) runs quality checks inside the container and returns a list of failure tuples to the orchestrator; it never spawns agents internally.
- An **orphan sweep** runs once at orchestrator startup; **collision detection** uses a per-branch `asyncio.Lock` held in `implement_phase` for the full duration of each `run_issue` call — from first worktree creation to final worktree teardown.
- **`detached_worktree`** is used by `planning_phase` (for the plan-sandbox worktree) and `preflight_phase` (for the pre-flight-sandbox worktree); **`branch_worktree`** is used by `merge_phase` (for the merge-sandbox worktree); **`_agent_worktree`** is used by `run_issue` in `implement.py` for Implementer and Reviewer worktrees — its cleanup is conditional on cancellation state, unlike the unconditional teardown in `detached_worktree` and `branch_worktree`. **`worktree_path`** and **`worktree_name_for_branch`** are the single authoritative path and name expressions used by all of the above.
- Host mounts per container: host repo → RO at `/home/agent/repo`; worktree → RW at `/home/agent/workspace`; `<host-repo>/.git` → RW at `/.pycastle-parent-git`; on Windows, gitdir overlay → RO over `/home/agent/workspace/.git`.
- A **Service** defines a Custom exception hierarchy so callers never handle raw subprocess exceptions; tests inject Default implementations from a test fixture and override per-test for error paths.
- **StatusDisplay** is a separate injectable in `Deps` alongside `Logger`; `Logger` owns file I/O, `StatusDisplay` owns the live terminal UI — they never overlap.
- **`Deps`** does not carry `env`; credentials are extracted from the environment in `main.py`, passed directly to `AgentRunner` at construction time, and are not accessible to any iteration-layer phase. Phase functions never reference `env` directly.
- Each phase module declares its own **per-phase dependency protocol** listing only its actual field accesses; `Deps` satisfies all of them structurally so the orchestrator passes it unchanged; tests construct minimal inline dataclasses with only the required fields. `_WorktreeDeps` in `worktree.py` is the established precedent for this pattern.
- Rich markup (e.g. `[red]...[/red]`) must never be embedded in a `StatusDisplay.print` message string; colouring is expressed exclusively via the `style` parameter (`"error"`, `"success"`, `"warning"`).
- A **status row** is created by `StatusDisplay.register` and removed by `StatusDisplay.remove`; phase rows are managed via the **`phase_row`** context manager — registered on entry and removed (with the phase outcome as the shutdown message) via `PhaseRow.close()`; agent rows are registered at container Setup and removed when the agent finishes or errors; the `rich` `Live` display is started on the first `register` call and stopped after the last `remove` call.
- All orchestrator-level terminal output (e.g. "Planning complete…") is routed through `StatusDisplay.print()` so `rich` can coordinate it with the live panel; bare `print()` calls are not used while a `StatusDisplay` is active.
- During the Work phase the container runner renders and injects the prompt, then owns byte chunking, byte-to-line splitting, log writing, and idle timeout detection via `WorkStream`; it passes the decoded NDJSON line stream and an **`on_turn` callback** to **`process_stream`**, which assembles assistant turns (invoking the callback for each), detects 429 error responses via `_check_usage_limit` and raises `UsageLimitError(reset_time)` immediately (where `reset_time: datetime | None` is parsed from the Claude message and converted to local time), unwraps the result envelope, and returns a typed `AgentOutput`; phases receive `AgentOutput` directly from `AgentRunner.run()` — no phase calls `parse()` or `assert_complete()`. Setup and Pre-flight phases produce no console output — their activity is reflected only in the body column of the agent status row.
- **`AgentRunner`** constructs a `DockerSession` (calling `build_volume_spec` to resolve volume paths) and a `ContainerRunner` (passing the session), then orchestrates the three lifecycle phases; it is the only caller of `build_volume_spec` and the owner of `CLAUDE_ACCOUNT_JSON` injection into the session.

## Example dialogue

> **Dev:** "If ruff, mypy, and pytest all fail at startup, do we file three issues?"

> **Domain expert:** "No — we pick the first failure by PREFLIGHT_CHECKS order and file exactly one issue via the **preflight-issue agent**. The agent explores the codebase, determines root cause, and decides the **HITL verdict**. The other failures surface in the next iteration's preflight."

> **Dev:** "What if the agent isn't sure whether a human is needed?"

> **Domain expert:** "It defaults to `ready-for-human`. The **HITL verdict** is read directly from the issue label — that label is the single source of truth. If it's `ready-for-human`, we return **PlanHITL** and exit. The operator goes to GitHub to see the filed issue."

> **Dev:** "And if it's `ready-for-agent`, how does the Implementer know it's starting from a clean state?"

> **Domain expert:** "The **safe SHA** was pinned when preflight passed. The Implementer's worktree is always created from that SHA — never from HEAD. So even if something lands on main between preflight and the Implementer spinning up, the agent starts from the verified-clean commit."

> **Dev:** "After the preflight fix merges, do we re-run preflight before planning the next iteration?"

> **Domain expert:** "Yes — every iteration starts with a **pre-planning preflight**. The preflight pulls the latest remote changes, pins the **safe SHA**, then runs quality checks. On pass, the iteration proceeds. There's no separate post-merge check."

## Flagged ambiguities

- **"preflight"** appears in two distinct contexts: the **Pre-flight phase** (an agent lifecycle phase that runs inside a container and returns failure tuples) and the orchestrator-level **preflight check** (which the orchestrator runs before planning and after merges). These are related but distinct — the Pre-flight phase is the mechanism, the orchestrator-level check is the policy that decides when to run it and what to do with failures.
- **"bug-report agent"** and **"bug-report.md"** are removed by the preflight refactor and replaced by **preflight-issue agent** and **preflight-issue.md**. Any reference to the bug-report agent in existing code, tests, or documentation refers to the old behavior.
