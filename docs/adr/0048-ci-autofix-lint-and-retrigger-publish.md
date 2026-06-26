# CI auto-fixes ruff and re-triggers publish, force-moving the release tag

On a push to `main` (or a `vX.Y.Z` tag), the publish workflow first runs `ruff format` + `ruff check --fix`. If the tree changes, it commits the fix, pushes with a **privileged token** (PAT/deploy key — the default `GITHUB_TOKEN` cannot re-trigger workflows), aborts the current run, and lets the re-triggered run do validation and publish. On a tag build it **force-moves `vX.Y.Z` onto the fixed commit** so the re-triggered run publishes to PyPI at the *same* version. `mypy` and unfixable ruff stay pure red gates — no fix, no publish.

## Why

Fixable lint reaching `main`/a tag previously turned the publish pipeline red and blocked the release for a class of problems a tool resolves mechanically. Auto-fixing commit-back keeps `main` green and the published artifact in sync with source, without a human round-trip.

## Decision detail

- **Scope:** push events only (`main` branch pushes + `v*.*.*` tags). Never PRs — the dominant flow is the agentic merge phase pushing straight to `main`, not PRs; tag builds are the only release path. PR auto-fix was explicitly left out.
- **Fixable set:** `ruff format` **and** `ruff check --fix`. This newly enforces formatting on `main` for the first time (CI previously ran only `ruff check`, no format gate) — a deliberate, accepted one-time reformat. `mypy` and non-autofixable ruff rules can never trigger a fix push; they fail loudly with no publish.
- **Re-trigger via privileged token:** the fix push must fire the publish workflow again, so it uses a PAT/deploy key, not `GITHUB_TOKEN`. Accepted security cost (privileged push credential in Actions secrets) — it is the only way to get the re-trigger.
- **Non-tag `main` push:** fix → commit on top → push to `main` → re-trigger → new dev version to testpypi. Version naturally differs; "same version" is irrelevant for dev releases.
- **Tag push:** fix → force-move `vX.Y.Z` onto the fixed commit → push tag → re-trigger → PyPI at the **same** version. `setuptools-scm` yields the clean `X.Y.Z` only when the tag points exactly at the built commit, so a fix commit on top would otherwise become `X.Y.(Z+1).devN` and route to testpypi instead of PyPI — re-tagging is the only way to keep the release version. The fix also fast-forwards onto `main`.
- **Tag invariant / simultaneous push:** the intended release workflow is `git push origin main vX.Y.Z` in one command — main and the tag land together. The tag branch in `ci-autofix.sh` therefore pushes to `main` **first**, then force-moves the tag. `set -euo pipefail` means a non-fast-forward rejection on the main push (race lost to the concurrent main-branch autofix run) exits the script before the remote tag is touched, leaving the tag on the original commit with no partial state on the remote. Manual recovery in that case: `git fetch --tags && git tag -f vX.Y.Z origin/main && git push origin vX.Y.Z --force`.
- **Force-move is safe** because the original tag never published: the test gate failed, so nothing ever reached PyPI under `X.Y.Z`. No consumer saw the moved-from commit.
- **Run handoff:** if the fix step changes the tree, the current run pushes and **aborts before validate/publish**, so it never builds/publishes the pre-fix tree; the re-triggered run validates and publishes. Push is gated on a non-empty diff. `ruff format`/`--fix` are idempotent, so the second pass has an empty diff and flows straight through — no infinite loop.

## Considered Options

- **Ephemeral fix (apply only to let the build pass, never commit).** Rejected: hides the violation and silently drifts the published artifact from `main` source.
- **Publish the fixed code in the same run (build the post-fix HEAD, no re-trigger).** Rejected: avoids a privileged token but cannot retag-for-PyPI cleanly and gives no real second-pass validation; the maintainer wanted the full pipeline re-run.
- **Default-token push, no re-trigger.** Rejected: the fix would land but the publish for that SHA would never fire (GitHub blocks recursive triggering from `GITHUB_TOKEN`), permanently drifting testpypi from `main` and never reaching PyPI for a fixed release.
- **PR-only auto-fix.** Rejected: misses the primary direct-to-`main` agentic flow and the tag release path entirely.

## Consequences

- A privileged push credential (PAT/deploy key) now lives in Actions secrets.
- CI authors commits on `main` and force-moves `v*` tags — surprising to a human watching; this ADR is the record of why.
- Formatting is now enforced on `main`; the first run reformats any currently-unformatted code.
- A tag release with a fixable lint slip self-heals to PyPI at the intended version instead of blocking.
- Near-simultaneous pushes (main + tag together) can race: both workflows start seconds apart and both try to push a ruff-fix commit. The push-order rule (main before tag) resolves this: the losing runner exits cleanly without touching the remote tag, so the only failure mode is the tag publish being skipped — not a mismatched or partially-moved tag. Recovery is a manual one-liner (see Tag invariant above). The race is rare and acceptable; the publish-to-PyPI path always requires a deliberate human re-push of the tag when it occurs.
