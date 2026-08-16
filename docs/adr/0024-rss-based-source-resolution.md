# ADR-0024: RSS-based Source Resolution for Web Research

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner
- **Tags:** security, integration, architecture

---

## Context

ADR-0023 established the Web Research capability as a two-stage pipeline (HTTP
fetch + isolated Fetcher-LLM extraction) behind a curated domain allowlist.
The implementation that followed (Phases 1–3) resolved user queries to
candidate URLs using a `query_patterns` field in each allowlist entry — URL
templates with a `{query}` placeholder that were filled with the URL-encoded
query and fetched as site-specific search pages.

In production the pattern-based resolution produces zero results against the
sources we care about. Three structural failure modes observed in the Phase 3
logs:

1. **JavaScript-rendered search pages.** Financial Times, Handelsblatt, and
   comparable institutional outlets return search pages whose article list is
   built client-side from a JSON API. The raw HTML returned to a non-browser
   client contains chrome and an empty results container. `trafilatura.extract`
   yields an empty string — there is nothing for it to extract.
2. **Automated-request blocking.** Bloomberg returns HTTP 403 to any request
   that does not present a full browser TLS/JA3 fingerprint and run the bot
   challenge. Reuters does the same via a Cloudflare interstitial (HTTP 401
   with an "enable JS" page body). No allowlist hint or User-Agent string
   changes this without moving into detection-evasion territory, which is
   out of scope.
3. **Query-URL guesswork.** The BaFin search URL in the current allowlist is
   a best-effort reconstruction from a publicly visible search page. It
   returns HTTP 404 because the site's real search endpoint differs in
   structure. Every retrieved search-URL template in the current allowlist
   is subject to the same silent-rot failure mode: the template breaks the
   moment the site's search UI is re-implemented.

These are not patterns-we-can-fix. They are structural properties of the
modern web: major publications ship SPAs, gate search behind bot protection,
or restructure URLs freely because the canonical entry point is their own
UI, not a URL API.

PortfoliFLOW is a pre-production, single-user system. We have the freedom to
remove the failing mechanism cleanly rather than carry it as dead weight, and
to replace it with a resolution mechanism that is aligned with how
institutional outlets actually publish machine-readable indexes.

This decision is security- and audit-relevant (BAIT AT 7.2 — IT-risk
management; ISO 25010 — Reliability: behaviour in the presence of environment
change; Security: unchanged from ADR-0023).

## Decision

The `WebResearchService` resolves candidate article URLs **exclusively from
RSS/Atom feeds** declared per allowlist entry. The pattern-based mechanism
is removed.

**Allowlist schema.** `query_patterns: list[str]` is replaced by
`feeds: list[str]` (required, non-empty). A global
`default_window_hours: int` is added at the top level. An optional per-entry
`window_hours: int` overrides the default for individual sources. The
domain-allowlist property of ADR-0023 is unchanged: feeds must themselves be
served from allowlisted hosts, validated at config load time.

**Resolution pipeline.** For each research call:

1. For every feed URL in every allowlist entry, fetch the feed via plain HTTP
   (same fetcher, same timeouts, same post-redirect allowlist re-validation
   as article fetches) and parse it with the `feedparser` library into
   structured `FeedItem` records (URL, title, description, publication date,
   source name).
2. Filter feed items by publication date against the effective time window.
   The default window is 72 hours. Items whose `pubDate` cannot be parsed
   into a timezone-aware datetime are dropped with a WARNING log entry —
   honesty over silent inclusion.
3. Combine surviving items from all feeds into a single candidate list.
   Present this list to a **Feed-Filter-LLM**: a new, tool-free,
   `send_one_shot_extraction`-based call whose system prompt
   (`docs/Feed_Filter_Prompt.md`) instructs it to return only the URLs that
   are substantively relevant to the query, ordered by relevance, capped at
   `max_articles`. The response is parsed against a `FeedFilterResult`
   pydantic model. Defence-in-depth: the service then filters the returned
   URLs to only those actually present in the candidate list — the LLM
   cannot introduce URLs.
4. For each pre-filtered `FeedItem`, fetch the article URL and run the
   existing Fetcher-LLM pipeline unchanged. Per-source failures are logged
   WARNING and skipped.

**No pattern fallback.** If a source cannot publish an RSS feed, it does
not belong on the allowlist under this ADR. Mixed-mode resolution (feeds
for some, patterns for others) is explicitly rejected — it reintroduces the
failure modes this ADR exists to eliminate.

**External contract stable.** `WebResearchService.research(query, max_articles)`
and the `web_research` tool keep the same public signature and tool class
(`READ_EXTERNAL_UNTRUSTED`). Only the internal resolution mechanism changes.
The `<external_content>` wrapper (ADR-0022), the Fetcher-LLM isolation
(ADR-0023), and the turn-scoped gating rules (ADR-0022) are unchanged.

## Rationale

- **RSS is the institutional publishing primitive for feeds.** Central banks,
  supervisors, and institutional news outlets publish RSS because regulators,
  journalists, and aggregators consume it. It is server-rendered, bot-
  friendly, and carries the metadata we need for cheap pre-filtering. It is
  also legally uncontroversial: RSS is explicitly published for syndication.
- **Pre-filtering on metadata is cheap and aligned with the threat model.**
  The Feed-Filter-LLM sees only titles and short descriptions — attacker-
  controlled content, but structured and tightly bounded. It is run with
  temperature 0.0, no tools, no history, a dedicated system prompt instructing
  it that the candidate text is data not commands, and a post-response
  defence-in-depth filter that rejects any URL not in the original candidate
  list. The isolation properties that ADR-0023 enumerates for the Fetcher-LLM
  apply identically here.
- **Shifting the Fetcher-LLM to pre-filtered inputs cuts cost and latency.**
  Instead of Fetcher-LLM running on every candidate from every feed, it runs
  only on items the filter judged relevant — typically a handful. Two LLM
  round-trips per research call become a fixed small number rather than
  scaling linearly with allowlist size.
- **Narrower coverage is a real tradeoff, honestly named.** RSS gives the
  most recent N items of a category, not a full-text historical search.
  Some sources we wanted (Bloomberg, Preqin, PitchBook) will not be
  reachable under this ADR. This is explicitly preferred to a mechanism
  that ships but does not work.
- **The time window is part of the audit surface.** "Last 72 hours" is a
  statement about what the tool can and cannot see. A reviewer can read it
  in the config file and understand the capability's temporal footprint at
  a glance.

## Alternatives Considered

- **Hybrid pattern + feed fallback.** Keep `query_patterns` alongside
  `feeds`, fall back to pattern resolution when feeds are empty or produce
  nothing. Rejected. Patterns are the failure mode we are removing; carrying
  them as fallback preserves the failure surface and introduces mode-
  switching that is hard to reason about. Clean cut is correct for a
  pre-production feature.
- **Headless browser for JS-rendered search pages.** Render the SPA with
  Playwright or similar and extract from the rendered DOM. Rejected.
  Large new dependency, significant CPU cost, moves us into bot-detection
  escalation (headless browsers are what bot-protection products fingerprint),
  and still does not solve the 403 Reuters/Bloomberg case.
- **Commercial search/news API (Tavily, Perplexity, Bing News, NewsAPI).**
  Rejected in ADR-0023 and the reasoning still holds: attacker-controlled
  snippets, no provenance guarantees, third-party dependency with terms and
  costs, and the allowlist concept becomes unenforceable because the API
  decides what reaches the pipeline.
- **Keyword-only pre-filter on feed metadata (no LLM).** Deterministic,
  cheap, no extra LLM call. Rejected because it is too imprecise for
  institutional terminology: a query about "DAX" misses articles that talk
  about "der deutsche Leitindex", and a naive keyword filter on the word
  "rate" picks up every unrelated corporate rating change. The filter
  call is a bounded, isolated LLM round-trip — the cost is real but small,
  and the quality difference is large.
- **Skip the pre-filter entirely, run Fetcher-LLM on every candidate.**
  Rejected as the original cost/latency profile we are moving away from —
  and it gives Shirley `max_articles` items chosen by feed order, not by
  relevance, which is markedly worse in practice.

## Consequences

### Positive

- Fetches produce real, structured results against current sources (verified
  at ADR-writing time: ECB, Financial Times, ESMA, BIS) rather than the
  zero-result outcome of Phase 3.
- The Fetcher-LLM is invoked only on LLM-selected relevant candidates; cost
  and latency scale with relevance-hits, not with allowlist size.
- Feed metadata (title, description, pubDate) is a cheap and useful
  relevance signal — a large quality jump over raw search-page extraction.
- The time-window default (72 hours) makes the tool's recency footprint
  legible to a reviewer and to Shirley's tool description.
- The implementation is legally uncontroversial: RSS is published for
  syndication.

### Negative

- Coverage is narrower than full-text search. A story published four days ago
  on a monitored source is invisible; a story on an outlet that does not
  publish RSS is invisible.
- Feed schemas vary. Atom versus RSS, published versus updated, full article
  body versus teaser-only, missing-or-unparseable `pubDate`. The service
  handles these cases with explicit logging and drops, not by best-guess
  filling — so some feeds (ESMA currently) will log "no usable pubDate"
  WARNINGs until the publisher fixes their feed.
- Every research call now makes up to N feed fetches plus one pre-filter LLM
  call in addition to the Fetcher-LLM fan-out. Feed fetches are cheap, the
  LLM call is small, but the total round-trip count per call increased.
- Adding a source requires verifying its feed URL in a browser before
  commit. A new process discipline — the previous session shipped a YAML
  that had never been verified against reality, which is what drove this
  ADR.

### Neutral / Follow-ups

- **Feed-Filter-LLM system prompt** (`docs/Feed_Filter_Prompt.md`) — a new
  reviewable artefact, drafted in the implementation prompt.
- **Audit-evidence surface** grows: per-call logs now record feed items
  pulled per feed, items surviving the time filter, items returned by the
  pre-filter, and articles yielding validated Fetcher-LLM output.
- **Publisher-side feed breakage** is now a monitorable failure class. Watch
  for WARNING-rate spikes on `fetch_feed` and on "no usable pubDate" drops.
- **Sources without public RSS** are a known gap. When a specific source
  becomes necessary, revisit: a narrow per-source adapter, or a commercial
  search API (with its own ADR).

## Implementation Notes

- Affected files:
  - `config/web_research.yaml` — full rewrite onto the new schema.
  - `services/web_research/allowlist.py` — remove `query_patterns`; add
    `feeds`, `window_hours`, `default_window_hours`, `get_effective_window`;
    enforce feed-URL allowlist at load time.
  - `services/web_research/fetcher.py` — add `fetch_feed`, `parse_feed`;
    existing `fetch_url` / `extract_text` unchanged.
  - `services/web_research/service.py` — rewrite `research()` onto the
    feed + pre-filter + article-fetch flow; add `_pre_filter_feed_items`
    and feed-filter prompt loader.
  - `services/web_research/models.py` — add `FeedItem`, `FeedFilterResult`;
    `WebResearchResult` unchanged.
  - `services/tools/web_research_tool.py` — update the LLM-facing tool
    description.
- New reviewable artefact: `docs/Feed_Filter_Prompt.md`.
- New dependency: `feedparser>=6.0`.
- Tests: `tests/services/web_research/test_allowlist.py`,
  `test_fetcher.py`, `test_service.py`, and new
  `test_feed_filter_prompt.py`. Removed tests that referenced
  `query_patterns` or `get_query_urls`.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Reliability (behaviour under
  realistic web-environment change — the pattern mechanism failed silently,
  the feed mechanism logs explicit per-feed success/failure), Security
  (unchanged from ADR-0023 and ADR-0022; the narrower fetch surface is
  additive defence, not a reduction), Maintainability (feed URLs change
  less often than search-page URLs; adding a source is a verified-in-browser
  + one YAML entry operation).
- **Regulatory references:** BAIT AT 7.2 (IT-risk management). The
  verification-before-commit discipline for new feeds is a documented
  control over the external-data surface. The per-call log enrichment
  (feeds fetched, items by stage) supports after-the-fact incident review.
- **Audit evidence:**
  - `config/web_research.yaml` — the feed allowlist, reviewable in full.
  - `docs/Feed_Filter_Prompt.md` — the pre-filter LLM's system prompt.
  - `docs/Fetcher_Prompt.md` — unchanged, still the Fetcher-LLM's system
    prompt.
  - The pydantic schemas in `services/web_research/models.py` —
    `FeedFilterResult` and `WebResearchResult`.
  - `services/web_research/service.py` — the orchestration source, linear
    and readable.
  - Per-call log entries capturing feed-stage counts (feeds fetched/failed,
    items before/after time filter, items sent to pre-filter, items
    returned, articles successfully extracted).

## Relationship to ADR-0023

ADR-0023 remains Accepted. Its architectural commitments — two-stage
processing, the Fetcher-LLM under `send_one_shot_extraction`, domain
allowlist, trust delimiters, synchronous execution, gating inheritance from
ADR-0022 — are unchanged. This ADR supersedes ADR-0023 only on the narrow
matter of **how candidate article URLs are resolved from a user query**:
the `query_patterns` mechanism is replaced by the RSS-feed + pre-filter
mechanism described above.

Per the ADR lifecycle rules in `docs/adr/README.md`, ADR-0023 is not
edited — its text continues to describe the architecture it was accepted
under, and this ADR is the authoritative record of the later refinement.

## References

- ADR-0023 (Web Research Capability — the architecture this ADR refines)
- ADR-0022 (Tool Trust Classes and Gating Policy — inherited unchanged)
- ADR-0012 (ToolRegistry as single seam — tool registration unchanged)
- ADR-0010 (AIService singleton — `send_one_shot_extraction` is reused
  for the Feed-Filter-LLM call alongside its existing Fetcher-LLM use)
- ADR-0016 (Three-line module-scope rule — this refactor touches only the
  files enumerated in *Implementation Notes*)
- `feedparser` library documentation (https://feedparser.readthedocs.io/)

---

## Revision History

| Date       | Author                       | Change        |
|------------|------------------------------|---------------|
| 2026-04-24 | PortfoliFLOW project owner   | Initial draft |
| 2026-04-27 | PortfoliFLOW project owner   | Status changed from Proposed to Accepted. RSS-based source resolution is implemented in `services/web_research/` (allowlist `feeds` field, Feed-Filter-LLM via `docs/Feed_Filter_Prompt.md`, pattern-based fallback removed). |
