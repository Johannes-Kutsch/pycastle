# Retire CI auto-fix; lint and format are red gates again

The publish workflow no longer repairs lint. `.github/scripts/ci-autofix.sh`, the `Apply ruff auto-fixes` and `Abort current run after auto-fix push` steps, the `CI_AUTOFIX_SSH_KEY` checkout input, and the push/pull-request checkout split are all deleted. The `test` job reverts to its exact pre-0038 shape — `checkout` → `setup-python 3.12` → `pip install -e ".[dev]"` → `ruff check` → `mypy src` → `pytest`. Fixable lint reaching `main` or a tag now fails the run and is fixed by hand. **Supersedes ADR 0038.**

## Why

ADR 0038 bought green-`main`-on-fixable-lint at three prices: a privileged push credential living in Actions secrets, CI authoring commits on `main` and force-moving `v*` tags, and a deliberate `exit 1` handoff that made a healthy run indistinguishable from a failed one at a glance. The analysis behind ADR 0046 measured the last cost directly — roughly a third of red publish runs were not test failures at all, and the autofix abort was a named contributor.

Then ruff 0.16.0 broke the mechanism outright. `ruff` was declared unpinned in the `dev` extra, 0.16.0 expanded its default rule set, and the codebase acquired ~618 violations overnight. `ci-autofix.sh` runs under `set -euo pipefail`, so `ruff check --fix` exiting non-zero for the remaining unfixable errors aborted the script *before* it could commit the fixes it had already made. The auto-fix mechanism failed silently rather than doing its job and letting the downstream `ruff check` report the rest. Commit `c3ed05ab` applied a band-aid — `select = ["E4", "E7", "E9", "F"]` to freeze the old default rule set, and `ruff check --fix || true` — which unblocked CI while disabling the thing the machinery existed to do.

The premise of ADR 0038 was that fixable lint reaching `main` is frequent enough to be worth automating away. With ruff pinned and the rule set written down explicitly (ADR 0052), it is not: the violation set is now a known, finite list rather than a moving target, and an agent that pushes unformatted or unlinted code gets a red run it can fix with one command.

## Decision detail

- **No format gate.** `ruff format --check` is deliberately *not* added to the workflow. ADR 0038 introduced format enforcement on `main` for the first time as a side effect of the autofix step; retiring the step returns formatting to unenforced, which is the pre-0038 behaviour the maintainer chose to restore exactly. `ruff format` still runs once during the ADR 0052 migration.
- **Single unconditional checkout.** With `ssh-key` removed, the push-event and pull-request-event `Checkout` steps are byte-identical, so they collapse back into one `actions/checkout@v4`.
- **Tags are never force-moved by CI.** A tag build with a lint slip fails and stays failed; recovery is a human retag, not a self-healing rebase.

## Considered options

- **Keep the autofix, pin ruff, and fix the `set -e` bug.** Rejected: it fixes the proximate failure but retains the privileged push credential, the CI-authored commits on `main`, the tag force-move, and the deliberate-`exit 1` noise in the run history — all to automate a fix that is one command and, post-0052, rare.
- **Keep the autofix for `ruff format` only, red-gate `ruff check`.** Rejected for the same reasons: formatting alone does not justify a privileged credential.
- **Retire the autofix but add `ruff format --check` as a new red gate.** Rejected by the maintainer in favour of restoring pre-0038 CI behaviour exactly. The accepted cost is recorded under Consequences.

## Consequences

- The `CI_AUTOFIX_SSH_KEY` secret is unused and can be deleted from the repository's Actions secrets. Until it is, it is a live privileged credential with no consumer.
- Formatting is unenforced on `main` again. Formatter drift will surface later as unrelated diff noise inside agent pull requests. This is a known, accepted cost of matching pre-0038 behaviour.
- The agentic merge phase pushes straight to `main`, so an agent that lands a lint violation turns publish red for everyone until someone fixes it. There is no longer a self-healing path.
- The `ci-autofix` integration tests (isolated git remotes covering branch, tag, clean-tree, and idempotent behaviour) lose their subject and are deleted with the script.
- ADR 0038's tag-race analysis, rebase-on-rejection recovery, and manual `git tag -f` recovery instructions are historical only.
