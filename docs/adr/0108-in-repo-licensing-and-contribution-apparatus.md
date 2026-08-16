# ADR-0108: In-Repo Licensing and Contribution Apparatus

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #052 — AGPL Public Release Track, gate 4
- **Tags:** licensing, agpl, cla, trademark, contribution, agpl-release-scope
- **Honours:** ADR-0008 (English-only codebase), ADR-0015 (Claude-assisted
  development workflow — the SPDX rollout is delivered as one Claude Code
  prompt, committed by the operator).

---

## Context

The repository is about to flip from private to public under roadmap item
#052. The settled strategy (recorded across the release-track sessions, not
yet in any ADR) is: AGPLv3 as the primary license, monetisation via dual
licensing (a commercial license relieving the AGPLv3 §13 obligation) and a
SaaS intelligence layer, and a three-layer protection model — contractual
(commercial license), trademark (EUIPO word marks **PortfoliFLOW** and
**Happy Computer Collective**), and architectural (the partner database
lives behind an API on portfoliflow.com, not in this repository). Two hard
ordering constraints govern the flip: the trademark filing must precede or
coincide with publication, and a CLA must be in place before the first
external contribution is accepted (non-reversible once missed).

As of the 2026-07-29 snapshot the tree contains **no** licensing apparatus:
no `LICENSE`, `CONTRIBUTING.md`, CLA, or `NOTICE` file; no copyright
headers; no `license` field in `pyproject.toml`; and a README that closes
with "Proprietary — all rights reserved." There are no vendored third-party
assets under `web/static/` — HTMX 1.9.12, Plotly.js 2.35.2, and Tabulator
5.6.1 are loaded from public CDNs and are not distributed with the
repository, so no third-party notices file is required. All 770 Python
files across `core/ services/ web/ modules/ cli/ bot/ scripts/ db/ tests/`
follow a docstring-first convention with no shebang lines.

## Decision

**D1 — License.** The repository is licensed under the GNU Affero General
Public License, version 3 only (`AGPL-3.0-only`). `LICENSE` contains the
verbatim, unmodified GNU AGPLv3 text. `pyproject.toml` declares
`license = {text = "AGPL-3.0-only"}` (table form; the PEP 639 SPDX string
form would require setuptools ≥ 77, while the build pins ≥ 68).

**D2 — Copyright holder.** Copyright is held by Sönke Pinkernelle
personally ("Copyright (c) 2025–2026 Sönke Pinkernelle") until a legal
entity exists. When an entity (GmbH/UG) is formed, copyright and the CLA
beneficiary ("the Maintainer") are migrated to it. The CLA is deliberately
a license *grant* (D4), not an assignment, so contributor grants survive
this migration without re-signing. The entity question is an operator/legal
decision and does not block the public release.

**D3 — Per-file headers.** Every `.py` file under `core/ services/ web/
modules/ cli/ bot/ scripts/ db/ tests/` carries a two-line header above the
module docstring:

```python
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle
```

`tests/` is included — one uniform rule is simpler than an exclusion.
Templates, static assets, and configuration files carry no per-file
headers; the `LICENSE` file and the README licensing section are the
central notice for them. The rollout is mechanical and idempotent (files
already tagged are skipped) and is delivered as a single Claude Code
prompt.

**D4 — Contributor License Agreement.** Contributions require a signed CLA
*before* the first merge — no exceptions. The CLA follows the license-grant
model (Apache ICLA-style, adapted), not copyright assignment: the
contributor retains copyright and grants the Maintainer a perpetual,
worldwide, irrevocable copyright and patent license that **explicitly
includes the right to relicense and dual-license** the contribution. Two
variants exist: `cla/individual.md` and `cla/entity.md`. Signing mechanics
v1 are lightweight and adequate for a solo-maintained repository: a signed
statement via email or a PR-description checkbox referencing the CLA file
and the contributor's commit. CLA-assistant automation is a follow-up under
the CI track (#054) and is not built now.

**D5 — CONTRIBUTING.md.** The contribution guide is honest about the
project's state. It states up front: (1) the CLA is required before any
contribution is merged, without exception; (2) the project is currently
single-maintainer and review capacity is limited; (3) issues are welcome,
pull requests by prior agreement; (4) which checks must pass before review
(pointing to the #054 CI contract; a placeholder line until that lands);
(5) architectural changes require an accepted ADR first, per the house ADR
discipline.

**D6 — Trademarks.** `TRADEMARKS.md` records that **PortfoliFLOW™** and
**Happy Computer Collective™** are trademarks of the copyright holder, that
trademark rights are *not* granted by the code license, and the naming
rules for forks: forks must be renamed and may state compatibility
factually ("based on PortfoliFLOW", "compatible with PortfoliFLOW") but may
not use the marks in a way that implies origin or endorsement. The README
gains a short "License & Trademarks" section replacing the proprietary
notice: AGPLv3, commercial license available (contact via portfoliflow.com),
™ notice.

**D7 — AGPL §13 source availability for the hosted instance.** The running
application links to its own source: both footers (`_partials/
statusbar.html` for the shell, `_auth_base.html` for the auth pages) render
a "Source code" link pointing to
`https://github.com/ProcessReengineer/PortfoliFLOW/tree/{BUILD_SHA}`,
pinning the link to the deployed revision via the existing `build_sha()`
context value; when `BUILD_SHA` is unset (`"dev"`), the link falls back to
the repository root. This is a template-level change and ships in the same
Claude Code prompt as the SPDX rollout, as a second, separately listed
step.

**D8 — Security policy (added 2026-08-16, pre-release).** `SECURITY.md` at the
repository root names a private reporting channel
(ProcessReengineer@happycomputercollective.org), scope, response expectation and
the coordinated-disclosure practice. Added as part of the apparatus before the
public flip; it changes no license or contribution term.

## Out of scope

- The commercial license *contract* — a bilateral document, drafted with
  counsel when the first prospect appears. This ADR records only that the
  dual-licensing mechanism exists and that the CLA preserves it.
- Trademark filing execution (#052 gates 1–3; operator).
- CI enforcement of the header rule (noted for the #054 track).
- Legal review of the CLA text — recommended before the repository flip;
  operator decision; does not block drafting.

## Consequences

- The repository can flip to public with a complete, coherent licensing
  story; #052 gate 4 becomes tick-ready once the files land.
- The AGPLv3 §13 trigger is satisfied by construction for the hosted
  instance — the source link is pinned to the deployed revision, so
  deploy discipline (setting `BUILD_SHA`) becomes a compliance detail,
  not just a diagnostic one.
- Every future `.py` file must carry the two-line header; until #054
  enforces this in CI, it is a review-discipline item.
- The CLA-before-first-contribution constraint is now recorded in an
  immutable decision record rather than session notes.
- Relicensing flexibility (e.g. a future license change or the commercial
  dual license) is preserved because all external contributions arrive
  under an explicit relicensing grant.
- No third-party notices obligation exists today; if a JS library is ever
  vendored into `web/static/`, a `THIRD_PARTY_NOTICES` file must be added
  in the same commit.

| Date       | Author                  | Change        |
| ---------- | ----------------------- | ------------- |
| 2026-07-29 | Claude (Opus) w/ owner  | Initial draft |
| 2026-08-16 | Claude (Opus) w/ owner  | D8 — security policy |
