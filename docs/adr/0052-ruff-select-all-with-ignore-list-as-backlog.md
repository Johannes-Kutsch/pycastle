# Ruff `select = ["ALL"]`, with the ignore list as the migration backlog

Ruff is configured with `select = ["ALL"]` and a two-block `ignore` list: a **permanent** block recording rules rejected by decision, and a **backlog** block listing rules that are enabled in principle but not yet clean. Each backlog entry is one ticket — delete the line, fix the findings, land green. The lint gate is therefore green at every commit while strictness ratchets up monotonically. `target-version` is `py312`, `requires-python` is raised to `>=3.12`, and `ruff` is pinned to `0.16.*` for the duration of the migration.

## Why

The band-aid `select = ["E4", "E7", "E9", "F"]` added in `c3ed05ab` froze ruff at its historical default rule set. That is the weakest gate ruff offers, on a codebase where agents push straight to `main` and lint is one of the few mechanical checks on their work. The goal is the opposite: maximum strictness, kept current.

Measured against the tree at the time of this decision, the options priced out as:

| | Findings | Character |
|---|---:|---|
| ruff 0.16 bare defaults | 582 | what the frozen `select` was hiding |
| `ALL` minus rejected families | ~1,700 | every correctness, security, and modernisation rule |
| `ALL` bare | 20,513 | the above, plus annotate and docstring the entire repo |

`ALL` bare is not "stricter" in any sense that catches defects: six families — `ANN` 5,809, `S101` 4,827, `D` 3,139, `COM812` 2,293, `E501` 636, `ARG` 548 — account for 94% of the difference, and every one of them flags the *absence of optional decoration* rather than a defect. `ALL` also emits internal-contradiction warnings the moment it is enabled (`D203` vs `D211`, `D212` vs `D213`).

The deny-list shape is chosen over growing an allow-list because it is the only one that satisfies "keep it up to date": under `select = ["ALL"]`, a ruff upgrade that introduces an entirely new rule family enables it automatically. An allow-list would silently never pick those up.

## Decision detail

**Permanently rejected**, with reasons that will otherwise be re-litigated:

- `D` (3,139) — mandated docstrings degrade into restatements of the signature.
- `COM` (2,293) — ruff's own documentation says it fights `ruff format`.
- `E501` (636) — `ruff format` already owns line width and cannot wrap long literals or URLs; the rule re-reports the formatter's own decisions.
- `TRY003` (174) — pushes toward a custom exception class per message. **ADR 0048 deliberately deleted pycastle's translated error types** to close a class of translation-gap bug; this rule pushes directly back. `EM101`/`EM102` apply the same pressure and are rejected with it.
- `PLR09xx` (109) — arbitrary numeric thresholds. `C901` is the better-calibrated version of the same idea and stays **enabled**.
- `CPY001` (187) — the repository has a `LICENSE`; per-file headers add nothing.

**Ignored in `tests/` only** — these fire almost entirely on test-suite idiom, and their production-code counts are small and interesting: `S101` (17 in `src/` vs 4,810 in `tests/`), `ANN` (77 vs 5,732), `ARG`, `PLR2004`, `S603`, `S607`, `S105`, `S106`, `S108`.

- `DTZ` is ignored in `tests/` because **ADR 0046 already rejected it**: the observed time bombs were timezone-*aware*, so `DTZ` misses them, and the suite legitimately contains absolute-datetime literals as injected `now=` inputs that are statically indistinguishable from the bomb. Measured confirmation: `DTZ` fires 0 times in `src/` and 24 times in `tests/`, entirely on the pattern ADR 0046 calls correct. It stays enabled in `src/` as a zero-cost tripwire.
- `SLF001` is deliberately **not** ignored in `tests/`. It is the mechanical enforcement of the standing rule that tests exercise only the public interface; suppressing it in tests to save four fixes would disable the one check that enforces it.

**`--unsafe-fixes` is never run across the tree.** This is not caution, it is measurement: `T201`'s unsafe fix *deletes the `print` statement*. Applied to `src/`, it removed 26 print calls across nine production modules — including `commands/init.py`, `commands/check.py`, `bug_reporter.py`, and `display/status_display.py` — leaving behind `if …: pass` branches, and turned a green suite into `77 failed, 2,692 passed`. Unsafe fixes are opt-in per rule, after reading the diff.

**`requires-python` rises to `>=3.12`.** `target-version = "py312"` was already set while `requires-python` said `>=3.11.3`, so `UP040`/`UP046` emit PEP 695 `type X = …` syntax that does not parse on 3.11. CI only ever tested 3.12, so the 3.11 claim was unverified and would have shipped broken. Either test 3.11 or stop claiming it; this ADR stops claiming it.

**Ruff is pinned to `0.16.*` during the migration**, loosening to `>=0.16` once the backlog block is empty. While dozens of families are in flight, a finding from a ruff upgrade is indistinguishable from a ticket regression, and that ambiguity costs an agent a session. Once the backlog is drained, an upgrade's findings are unambiguous and the interruption is the intended behaviour.

**Rule resolutions decided up front**, so that no backlog ticket requires a judgment call:

- `PLW1510` → add `check=False`. All three `src/` sites already inspect `returncode`; `check=True` would be wrong at every one.
- `B904` → `raise … from exc`. Affects traceback chaining only.
- `TRY004` → `# noqa` at all three sites. They raise on an agent violating the output protocol — a domain failure, not a caller type error — and `RuntimeError` is the correct semantic.
- `BLE001` → narrow the `except` clause. Binding as `except Exception as exc` does **not** satisfy the rule; only `logging.exception(...)` or re-raising does, and pycastle reports through the status display, not `logging`.
- `SIM105`/`S110` → `contextlib.suppress`, **sequenced after `BLE001`**. `contextlib.suppress(Exception)` triggers no ruff rule whatsoever, so converting first would launder blind excepts past `BLE001` without anyone deciding anything.

## Considered options

- **Leave `[tool.ruff.lint]` absent and inherit ruff's defaults**, as the originating issue proposed. Rejected: that is precisely the mechanism that broke CI — an unpinned tool whose default rule set is a moving target. The enforced gate would be "whatever ruff shipped on the day CI resolved it."
- **Grow `select` one family at a time from the frozen set.** Same ticket count, but never picks up new rule families on upgrade, contradicting the goal of staying current.
- **`select = ["ALL"]` with no ignore list, fixed in one pass.** 20,513 findings, ~17,600 of them hand-edits. Not a session; a quarter.
- **Track the backlog in the issue tracker rather than in `pyproject.toml`.** Rejected: the tracker and the config would drift, and the gate reads the config. Keeping the backlog in the file the gate reads makes drift impossible.

## Consequences

- `pyproject.toml` carries a large, deliberately untidy `ignore` list for the duration. The two-block comment structure is load-bearing — deleting a line from the wrong block silently reverses a decision recorded here.
- Roughly twenty follow-up tickets exist before the backlog block is empty. Until then, "ruff is green" means "green against the rules enabled so far," not "green against `ALL`."
- `BLE001` (38 findings across 15 files) is the only family requiring an agent to read what each `try` body can actually raise. It is split across several tickets by file cluster rather than shipped as one.
- `T201` (26) and `PLC0415` (67) are enabled in principle but blocked on architecture work — routing output through `display/`, and resolving the `session_planning` ↔ `provider_session_adapter` import cycle respectively. Their backlog entries are gated on that work, not on lint effort.
- The local development virtualenv must be rebuilt on Python 3.12; once `requires-python` bumps, `pip install -e ".[dev]"` refuses to install on 3.11.
