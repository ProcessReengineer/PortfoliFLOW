# Changelog

All notable changes to PortfoliFLOW are recorded in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions use
calendar versioning (`YYYY.MM.N`). Planned work is in
[`docs/roadmap.md`](docs/roadmap.md).

## [Unreleased]

### Added

- The Voice call card loads the vendor's current model list and the account's
  voices on request, behind the Model and Voice fields; deprecated models and
  voices with live moderation (which an agent cannot use) are named but not
  offered. The fields stay free text.
- Shirley takes a live voice call in the web chat (ADR-0137, roadmap #077).
  A vendor-hosted conversation layer (ElevenLabs Agents) carries Shirley's
  persona and knows nothing about the book; every fact comes through one
  read-only tool, `ask_shirley`, which runs an ordinary Shirley turn in the
  user's session and relays the answer — interruption included. The call's
  transcript lands in the chat history with a *Call* chip, answers that needed
  a lookup appear as the usual bubble and chart. Per-tenant opt-in on the new
  **Voice call** Admin card with the tenant's own key; the vendor agent is
  provisioned lazily from a definition in the repository and kept up to date
  by hash. Audio and transcript go to the vendor; the card and handbook §8a
  say so. The browser SDK is vendored and loaded on the first call only.
- Shirley's voice messages no longer depend on one vendor (ADR-0136, roadmap
  #076). Speech-to-text and text-to-speech are chosen per tenant and per half
  on the Voice cards: any server speaking the OpenAI audio wire —
  including a local engine on the tenant's own hardware, keyless — ElevenLabs
  natively, or OpenAI until its speech models retire (text-to-speech
  2027-01-06, transcription 2027-02-26). Speech-to-text alone is a supported
  setup; the Voice card shows the state of each half. Telegram voice replies
  are transcoded to Ogg/Opus with the host's `ffmpeg` when a provider delivers
  another container; `ffmpeg` is optional and reported by the installer.
  `docs/deploy/voice-local-engines.md` lists local engines and their licences.
- On/off settings on the Providers & Credentials cards (Voice, Telegram,
  Provider channel) are switches; anything other than `true` / `false` is
  refused on save.

### Changed

- There is no voice provider default any more. A deployment whose `.env` or
  tenant rows name `openai` as provider behaves as before; one that had
  deleted `VOICE_STT_PROVIDER` / `VOICE_TTS_PROVIDER` and relied on the former
  code default finds that half inactive after the upgrade until a provider is
  chosen on the Voice cards or in `.env`. `VOICE_TTS_BASE_URL` is new.
- `/static` is served with `Cache-Control: no-cache`: assets stay cacheable but
  are revalidated on every use, so a redeployed script or stylesheet is live
  on the next page load (an unchanged file costs one 304).

## [2026.10.0] — 2026-10-12

### Added

- `AGENTS.md`, the rules file for coding agents, and `docs/glossary.md`, the
  canonical glossary of project terms.
- This changelog.
- The Watch Desk writes a briefing at every check — markets and press against
  the whole book, then what is under observation — kept with the check that
  wrote it and shown on the Briefing with its sources. Watch points can be
  switched on and off per family and set to a sensitivity preset (Quiet,
  Standard, Nervous); switching a quota family off asks first and leaves a
  standing note. The Journal lists every check, silent ones included, by day
  with filters. "Discuss briefing with Shirley" hands a briefing to Shirley in
  place.

### Changed

- Database-backed tests run against their own PostgreSQL container with its
  data in RAM (`compose.test.yml`, port 5433; ADR-0135), never the
  development database. A run migrates it to head by itself, no longer
  empties the development database, and no longer needs a
  `portfoliflow bootstrap` afterwards. Start it with
  `podman compose -f compose.test.yml up -d`.
- The Watch Desk's three views — Briefing, Watch points (the former
  Calibration) and Journal — are drawn from the shared component vocabulary:
  the check clock in the view head with the cadence behind it, the watch list
  with utilisation meters, findings as toned panels with a peer decision set,
  switch-off and retire confirmations inline, and the Journal by day with the
  written briefing beneath each check.
- `docs/roadmap.md` is now a steering document: active work, ranked next
  steps, ideas, decisions against, and a record of what shipped.
- `docs/architecture.md` is reconciled with the code.
- `readme.md` is renamed to `README.md`.
- The AI assistants' runtime prompts moved from `docs/` to `prompts/`.
- CI actions are pinned by commit SHA and watched by Dependabot; the fast tier
  runs on Ubuntu 24.04 (Python 3.11 and 3.13) and macOS 26 (Python 3.13).
- The web server reads its configuration through `WebSettings` instead of raw
  environment variables; an invalid `WEB_MAX_UPLOAD_SIZE_MB` now refuses
  startup.
- The sidebar no longer shows the signed-in e-mail address; it appears as the
  tooltip of the Sign out button. The navigation is the sidebar's scroll
  region and scrolls without a visible scrollbar.
- Charts show the Plotly toolbar only while the pointer is over them.

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
