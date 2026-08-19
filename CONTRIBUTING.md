# Contributing to PortfoliFLOW

Thank you for your interest in PortfoliFLOW. Please read this page in full
before opening a pull request — it is short, and it is honest about where
the project stands.

## The short version

1. **A signed CLA is required before any contribution is merged. No
   exceptions.** See [Contributor License Agreement](#contributor-license-agreement).
2. **The project is currently single-maintainer** and review capacity is
   limited. Response times on pull requests may be slow.
3. **Issues are welcome. Pull requests by prior agreement.** Open an issue
   describing what you want to change and wait for a go-ahead before
   investing significant work.
4. **Architectural changes require an accepted ADR first.** See
   [ADR discipline](#adr-discipline).

## Contributor License Agreement

PortfoliFLOW is licensed under the AGPLv3 and is additionally offered under
a commercial license (dual licensing). To keep this model legally sound,
every contributor must sign a Contributor License Agreement before their
first contribution is merged:

- Individuals: [`cla/individual.md`](cla/individual.md)
- Companies and other legal entities: [`cla/entity.md`](cla/entity.md)

The CLA is a **license grant, not a copyright assignment** — you keep the
copyright in your contribution and grant the Maintainer the rights needed
to distribute it under the AGPLv3 and under commercial license terms.

**How to sign (v1, manual):** send an email to the Maintainer at
ProcessReengineer@happycomputercollective.org (project site:
[portfoliflow.com](https://portfoliflow.com)) or add the following
statement to the description of your first pull request:

> I have read and agree to the PortfoliFLOW Contributor License Agreement
> (`cla/individual.md`, as of commit `<commit SHA of the CLA file>`).
> Signed: `<full name>` `<email used in your commits>` `<date>`

For entities, an authorised signer must send the completed schedule from
`cla/entity.md` by email before any covered employee's contribution is
merged. Automated CLA checking is planned as a CI follow-up; until then the
Maintainer verifies signatures manually.

## Issues

Bug reports and feature discussions are welcome. For bug reports, include:
the affected area/module, reproduction steps, expected vs. actual
behaviour, and (where relevant) the migration revision and `BUILD_SHA`
shown in the application footer. Please do not include real portfolio data
in issues. Security problems: see `SECURITY.md` — never open a public issue
for them.

## Pull requests

- Agree on scope first via an issue.
- Sign the CLA (see above) — merges are blocked without it.
- One concern per pull request. Small, reviewable diffs are far more
  likely to be merged.

### Checks that must pass

A green `ci.yml` run (lint, typecheck, fast test tier) is the merge
contract — pull requests are only merged on green. The full suite
additionally runs on every push to `main` (`full-suite.yml`).

Before pushing, the local pre-flight expectations are:

- `ruff check .` passes (100-character line length, `py311` target),
- `pytest` passes locally against a development database
  (see `db/README.md`),
- new `.py` files carry the two-line SPDX header used across the codebase.

### House rules

The full rule set lives in `CLAUDE.md` and `docs/architecture.md`. The
ones contributors most often trip over:

- English everywhere in code, comments, and documentation (ADR-0008).
  German is permitted only as domain data values.
- Python 3.11+ with modern type syntax; Google-style docstrings on all
  public APIs; all exceptions subclass `PortfoliFlowError`.
- `services/analytics/` is pure: no database access, no web-framework or
  GUI coupling. This is enforced by a regression test.
- Adding a module must touch at most three lines in existing files
  (ADR-0016).
- Conventional Commits (`feat(scope): …`, `fix(scope): …`, `docs(scope): …`).

## Shell scripts

Every file in `scripts/*.sh` must run on **bash 3.2** and **BSD
userland**, because macOS ships `/bin/bash` 3.2 and the documented
install one-liner executes under whichever bash the user has
(ADR-0124 §2.1). Bash parses a function body at definition time, so a
single bash-4 construct anywhere in a file breaks it before the first
line runs.

Not available, anywhere in the file: `declare -A`, `${var,,}` /
`${var^^}`, `mapfile` / `readarray`, `${var@Q}`, `;&` / `;;&`, `|&`,
`coproc`, `read -i`, `printf %(…)T`, negative array subscripts,
`local -n`; and in userland `sed -i` without a backup suffix,
`head -n -N`, `date -d`, `grep -P`, `realpath`, `readlink -f`,
`stat -c`, `seq`, GNU `timeout`. Indexed arrays, `[[ ]]`, `$(( ))`,
`local`, `printf`, `read -r -s`, `tr`, `awk`, `df -Pk` and process
substitution are all fine.

`shellcheck -s bash` must be clean.

## ADR discipline

Architectural decisions are recorded as Architecture Decision Records
under `docs/adr/` (thematic index in `docs/adr/README.md`). Accepted ADRs
are immutable historical records; corrections happen in successor ADRs.
**Any change that alters an architectural decision needs an accepted ADR
before implementation code is written.** If you are unsure whether your
change is architectural, ask in the issue first — it is a cheap question.

## Licensing of contributions

By submitting a contribution you confirm it is your original work (or that
you have flagged third-party material as described in the CLA) and that it
is submitted under the terms of the CLA. Trademark use is governed
separately by [`TRADEMARKS.md`](TRADEMARKS.md).
