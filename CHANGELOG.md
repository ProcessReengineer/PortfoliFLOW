# Changelog

All notable changes to PortfoliFLOW are recorded in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions use
calendar versioning (`YYYY.MM.N`). Planned work is in
[`docs/roadmap.md`](docs/roadmap.md).

## [Unreleased]

### Added

- `AGENTS.md`, the rules file for coding agents, and `docs/glossary.md`, the
  canonical glossary of project terms.
- This changelog.

### Changed

- `docs/roadmap.md` is now a steering document: active work, ranked next
  steps, ideas, decisions against, and a record of what shipped.
- `docs/architecture.md` is reconciled with the code.
- `readme.md` is renamed to `README.md`.

### Removed

- `CLAUDE.md`, replaced by `AGENTS.md`.

## [2026.09.1] — 2026-09-29

### Added

- A model setting of its own for Shirley's web research (`RESEARCH_MODEL`, or
  per tenant under Admin → Providers & Credentials), resolved per call like the
  other AI functions.

### Changed

- A major overhaul of the user interface: a new application shell and design
  system, one view per Section, and Shirley available beside every view. The
  Transactions Area is the first Area in the new design.
- Simpler setup of the AI assistants and the Telegram bot.

## [2026.09.0] — 2026-09-13

### Added

- The Transactions Area: trade tickets carried from draft through proposal and
  approval to booking, settled against a cash position, with blotter and
  history views and a pre-trade impact preview.
- A recommended-provider list for trade tickets, published as a signed
  directory.

## [2026.08.1] — 2026-08-25

### Added

- A one-line installer (`scripts/install.sh`), including a `--doctor` mode that
  checks the prerequisites and changes nothing.

## [2026.08.0] — 2026-08-16

The initial public release, under the GNU Affero General Public License v3.

### Added

- **Front Office:** a portfolio overview with headline figures and charts,
  per-investment charts, statistics, and portfolio analysis with an efficient
  frontier.
- **Back Office:** strategic asset allocation, benchmarks and attribution, and
  investment-limit monitoring for SAA and AnlV limits.
- **Investor Communication:** the Portfolio Review, for the whole book and for
  single investments.
- **Planning Desk:** cash flow planning and scenario analysis.
- **Watch Desk:** Irene, a background analyst that checks the book on a
  schedule against configurable watchpoints — limits, prices, FX, NAV
  freshness, liquidity and press — and raises findings.
- **Cases:** decision work opened from a finding or by hand, with a timeline,
  pinned documents, scenarios and Shirley excerpts, closed with a note.
- **Assistants:** Shirley, the AI assistant, with read access to the book
  through tools, image input and voice; the Report Scraper for GP quarterly
  reports; web research over curated news feeds.
- **Data:** the Excel import; live market data (Yahoo, OpenFIGI identifier
  resolution, a Bloomberg Desktop API adapter that stays off until validated
  live); a position model for listed instruments; multi-currency books with a
  functional currency per tenant; cash as an asset class.
- **Platform:** multi-tenancy with row-level security; owner, member and
  auditor roles; tenant user management; super-admin operations; scoped
  settings with an encrypted credential vault; a built-in scheduler for the
  periodic checks; a Telegram bot per tenant.
