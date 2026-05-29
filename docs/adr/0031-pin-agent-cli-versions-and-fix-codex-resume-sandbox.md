# Pin agent CLI versions and fix codex resume --sandbox rejection

The Codex CLI's `exec resume` subcommand does not accept `--sandbox` - sessions inherit sandbox config from the original run. `build_command` appended `--sandbox danger-full-access` unconditionally, so every codex resume attempt failed immediately. Fixed by emitting `--sandbox` only on `RunKind.FRESH`.

Separately, the supported agent CLIs (`@openai/codex`, Claude Code, and OpenCode) were installed unpinned (`npm install -g @openai/codex`, `curl ... | bash`, or latest-equivalent), meaning any upstream release could silently break agent runs. The default Dockerfiles now pin tested versions: codex-cli 0.134.0, Claude Code 2.1.152, OpenCode CLI 1.15.12.

The same pinning policy applies to planned OpenCode support. `OpenCodeService` depends on the OpenCode CLI's JSON event shape, session-id resume contract, model selection flags, and usage-limit messages. Pycastle therefore pins the OpenCode CLI version in the agent image/runtime setup rather than installing whatever version is latest at build time.

## Considered Options

**Resume sandbox fix**: (a) pass via `-c sandbox=danger-full-access` on resume, (b) omit entirely. Chose (b) - the session already stores its sandbox config from the original fresh run; injecting it via `-c` is speculative and risks conflicting with stored session state.

**Amendment: issue-filing roles bypass Codex's inner command sandbox.** Issue #1151 exposed a distinct resume-time failure: an improve PRD session had command-execution access, but every command failed before execution with `bwrap: No permissions to create a new namespace`. The agent could not run even `pwd`, so it could not create the GitHub issue body file or call `gh issue create`; after protocol reprompts it ended with `<promise>BLOCKED</promise>`, which correctly surfaced as `protocol_error`.

The chosen contract is role-scoped: Codex issue-filing roles that require shell/`gh` (`improve`, `preflight_issue`, and `failure_report`) use Codex's no-inner-sandbox automation flag (`--dangerously-bypass-approvals-and-sandbox`) instead of relying on Codex's `bwrap` sandbox inside the already-isolated pycastle container. The no-source-edit contract remains role-owned: Claude enforces it with `--disallowedTools "Edit Write NotebookEdit"`; Codex v1 enforces it by prompt contract plus pycastle's container/worktree lifecycle because the Codex CLI does not expose an equivalent per-tool disallow list. This is not user-tunable; tool/permission surface is an agent-role contract, not a stage preference.

Alternatives considered: (a) route issue-filing roles to Claude first, rejected because Codex must remain a first-class default stage candidate; (b) make improve/slice issue filing host-owned through `GithubService`, deferred because slice filing has complex body, dependency, and sub-issue semantics; (c) give all diagnostic roles full mutation access, rejected because the necessary capability is shell/`gh`, not source edits, and failure-report often inspects another role's preserved failure evidence.

**Version pinning location**: (a) hardcoded in Dockerfiles, (b) Python constants passed as Docker build args. Chose (a) - version pins are a build-time concern; consuming projects that need different versions override with their local Dockerfile copy. No templating overhead.

**OpenCode version policy**: (a) pin the OpenCode CLI like Claude and Codex, (b) install latest. Chose (a) - structured output and resume semantics are part of pycastle's runtime contract, so reproducibility matters more than automatically receiving upstream CLI changes.
