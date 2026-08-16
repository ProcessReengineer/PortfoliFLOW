# ADR-0109: Lint, Typecheck and CI Contract

- **Status:** Accepted (2026-07-29)
- **Date:** 2026-07-29
- **Deciders:** Soenke (ProcessReengineer)
- **Roadmap:** #054 (blocks #052 gate 5)

## Context

The AGPL public release (#052) makes the repository a public trust
artefact. Today the full test suite (~2 h, per the Strand-1 closure §5)
runs only on the operator's machine; `[tool.ruff]` configures only line
length and target version with the default rule set; pre-commit carries
only the two artefact-regeneration hooks; no typechecker runs anywhere.
A public repository without visible, passing CI undermines the trust
argument the release exists to make.

Constraints found in the tree: DB-backed tests bind the compose
Postgres through `tests/_db_fixtures` and skip gracefully when
`DATABASE_URL` / `DATABASE_URL_SUPERUSER` are unset; the migration
roundtrip suites leave the database downgraded; ~149 existing `# noqa`
directives reference rule families outside the selected set; the
purity-guarded packages are `services/analytics/`, `services/overlay/`,
and `services/market_data/`. Two pre-existing flakes (migration-
roundtrip downgrade; AI-service singleton pollution in combined runs)
are tracked for a dedicated fix run (Chat D, #052 gate 6) that has not
yet landed.

## Decision

1. **Ruff lint.** `[tool.ruff.lint] select = ["E","F","W","I","UP","B",
   "SIM","RUF"]`. No `D` (docstring culture is enforced socially, per
   ADR-0007 review practice), no `ANN`, no `S`. `external = ["ARG",
   "BLE","PL","PT","SLF"]` preserves existing intent-documenting noqa
   directives against RUF100. Any single rule with >50 hits at adoption
   time is placed in `ignore` as **staged adoption**. This ADR fixes
   the mechanism; the authoritative inventory of staged rules and of
   the DataStore per-file-ignores lives in `[tool.ruff.lint]` itself
   (one comment per entry) and in the adoption commit — deliberately
   delegated so the accepted ADR needs no retroactive edit when the
   adoption run fills the lists. Removal of staged entries rides #054's
   post-release note; the DataStore block (`core/data_store.py`,
   `modules/**`, `services/reporting/**`; #035, ADR-0094 §5) rides #035
   — it is scheduled for decommission, not modernisation.
2. **Format.** `ruff format` is adopted in one dedicated whole-tree
   commit — `style: adopt ruff format (mechanical, no logic changes)`
   — recorded here as a history event. Pre-commit gains pinned `ruff`
   and `ruff-format` hooks ahead of the artefact hooks.
3. **Typecheck.** pyright in basic mode on typing islands only: the
   three purity-guarded packages `services/analytics/`,
   `services/overlay/`, `services/market_data/`. No mypy. Whole-tree
   strict typing is a post-release note inside #054.
4. **CI (GitHub Actions), two workflows.**
   - `ci.yml` — every push and PR: ruff check, ruff format --check,
     pyright islands, fast test tier
     (`pytest -m "not integration and not timing"` with
     `tests/repositories`, `tests/services`, `tests/auth`, `tests/web`
     ignored and no database configured; remaining DB-bound tests
     self-skip by fixture design; the pure regression-guard family
     stays in). Job timeout 15 minutes; Python 3.11 (the published
     floor); pip cache keyed on `pyproject.toml`.
   - `full-suite.yml` — push to `main` plus manual dispatch; a nightly
     cron is activated with the public flip (#052 gate 8), not before
     (GitHub Actions minutes are metered while the repository is
     private). Postgres 16 service container, app role applied from
     `db/init/01-create-app-role.sql`, `alembic upgrade head`, the
     complete suite minus `tests/regression`, then the regression-
     guard and migration-roundtrip family as the final named job step
     (it is the public architectural contract, and roundtrips leave
     the schema downgraded). Timeout 180 minutes; concurrency group
     prevents parallel DB-backed sessions. `continue-on-error: false`.
     Until Chat D lands, the two known flake signatures are rerun-once
     exceptions: a failure matching them is re-dispatched once before
     being treated as red.
5. **Coverage: declined for the release.** No pytest-cov, no badge —
   guard-based enforcement over percentage optics. Revisit note in #054.
6. **Contributor contract.** A green `ci.yml` run is the merge
   contract. This sentence replaces the placeholder in
   CONTRIBUTING.md ("Checks that must pass").

## Consequences

- Every push gets a sub-15-minute verdict; the ~2 h suite runs where
  it is affordable (`main`, later nightly) without gating iteration
  speed.
- The typecheck contract is honest: it claims exactly what the purity
  guards already enforce structurally, and can expand island by island.
- One mechanical format commit enters history; `git blame` across it
  requires `--ignore-rev` (the commit hash is recorded in
  `.git-blame-ignore-revs`).
- Staged-adoption ignores and the DataStore per-file-ignores are debt
  made visible in config rather than debt hidden in diffs; their
  removal rides #035 and the post-release typing note.
- The config file, not this ADR, is the living inventory of staged
  rules — by design, so immutability holds without successor ADRs
  for what is operational rather than architectural.
- The rerun-once exception is temporary by construction: Chat D's
  closure deletes it (a config/comment change, not an ADR amendment —
  the exception is operational, the contract itself is unchanged).
