# ADR-0098: Computed-NAV Materialisation and Live-Ingest Write-Path Re-Routing

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** PortfoliFLOW project owner
- **Supersedes / amends:** builds on ADR-0097 (position model schema); leaves the ADR-0092 conditional-upsert semantics unchanged and extends the `ingest_origin` value set additively; redeems the `basis='computed'` definition of ADR-0079
- **Implements roadmap item:** #038 — Position Model (Transactions, Holdings, and Unitised Valuation)
- **Tags:** market-data, ingest, materialisation, nav, excel-precedence, regression

---

## Context

ADR-0097 fixes the source layer of the unitised position model: a
transaction ledger (`position_transactions`), a per-unit price series
(`instrument_prices`), pure holdings derivation, and
`investments.valuation_mode`. This ADR fixes the two remaining moving
parts:

1. **Materialisation** — turning `holdings × price` into
   `investment_navs` rows with `basis='computed'`, so every consumer of
   the NAV-series contract (analytics, charts, statistics, limits,
   Irene, reporting — finding F3) works unchanged.
2. **Write-path re-routing** — sending `SeriesKind.NAV_PRICE` per
   `valuation_mode` into `instrument_prices` instead of
   `investment_navs`, closing the latent P0 (finding F1: per-share
   prices in a position-value column) properly and retiring the S0
   interim guard. Finding F6 (per-share dividends ingested unscaled
   into position-level `investment_cashflows`) is closed at the same
   routing point by holdings-scaling.

The load-bearing constraint throughout: **the ADR-0092 Excel-precedence
conditional upsert is not altered.** `'excel'` and `'manual'` rows are
never mutated by any platform writer; the materialisation joins the
existing writer set exactly as ADR-0092 enumerated it would.

## Decision

### 1. `ingest_origin` gains `'system'`

The `investment_navs.ingest_origin` CHECK set extends from
`('excel','live','manual')` to `('excel','live','manual','system')` in
this ADR's migration (`b025` or the then-current next free slot; one
concern per migration — ADR-0097's tables land in `b024`).

- `'system'` marks a row **written by the platform's materialisation
  service** — neither imported, nor provider-delivered, nor
  operator-entered.
- `basis` and `ingest_origin` stay orthogonal, per the standing
  invariant: `basis='computed'` states *how the number was formed*
  (holdings aggregation, ADR-0079); `ingest_origin='system'` states
  *which writer channel produced the row*. Materialised rows carry
  both. The `basis` column is never overloaded as an origin marker.
- Precedence order, strongest first: `'excel'` > `'manual'` >
  `'system'`. The materialisation refreshes **only its own `'system'`
  rows** (conditional upsert `... WHERE existing ingest_origin =
  'system'`, structurally identical to the ADR-0092 `upsert_live`
  guard). A conflicting `'live'` NAV row cannot legitimately exist for
  a unitised investment (the ADR-0097 §6 mode-flip deletes them; the
  §3 re-routing prevents new ones); if one is nevertheless
  encountered, the materialisation **skips it and logs a warning** —
  it never deletes or overwrites outside its own origin.

### 2. The materialisation service

A DB-writing service module
`services/investments/nav_materialisation.py` — deliberately **not**
under `services/analytics/` (which stays DB-free) and not under
`services/market_data/` (which stays provider-only and DB-free). The
pure value computation (`holdings × price` per date) is delegated to
the DB-free `services/investments/holdings.py` (ADR-0097 §4); the
service owns only reading sources, classifying, and writing.

**Semantics.** For one unitised investment, the materialised set is:
for every `as_of_date` carrying an `instrument_prices` row on or after
the first ledger date, with derived holdings `> 0` on that date, one
`investment_navs` row:

```
nav_value  = holdings(as_of_date) × price(as_of_date)
currency   = investment currency (equal by ADR-0097 §5)
nav_kind   = 'actual'
basis      = 'computed'
ingest_origin = 'system'
source     = 'computed:units×price'
created_by = the acting user; the market-data system actor (ADR-0093)
             when triggered by live ingest
```

Plan rows are never materialised: `nav_kind='plan'` series remain
value-based book rows outside this service's scope (F5; pinned in the
compatibility annex for ADR-C).

**Classify-then-write idempotency**, mirroring
`_ingest_live_nav_price`: the service reads the investment's existing
`actual` NAV rows once, classifies each target date, and

- inserts where no row exists;
- updates its own `'system'` row only when the value actually changed
  (a value-equal re-run is a counted no-op — `updated_at` untouched,
  re-runs byte-identical);
- skips `'excel'` / `'manual'` (precedence) and `'live'` (warning, §1)
  rows;
- **deletes stranded `'system'` rows** whose date no longer belongs to
  the materialised set (a backdated transaction edit, a price
  deletion, or a holdings-to-zero sale can shrink the set; only
  `'system'`-origin rows are ever deletion candidates).

Every run reports per-outcome counts (`inserted / updated / noop /
skipped_excel / skipped_manual / skipped_live / deleted`), logged like
the `LiveIngestReport`.

### 3. Trigger mechanics: synchronous, in-transaction

Materialisation runs **synchronously inside the caller's transaction**
at the write choke-points, not via a tick (handover §6.4 resolved):

- after a `position_transactions` write/edit/delete (recompute from the
  earliest affected date),
- after an `instrument_prices` write (live ingest, manual entry),
- on the ADR-0097 §6 mode flip (initial full materialisation).

Rationale: the computation is deterministic and cheap (one
investment's series — a cumulative sum and a join); running it in the
same transaction guarantees prices/ledger and computed NAVs can never
be observed disagreeing; the classify-then-write semantics are
idempotent, so a repeated trigger is harmless; and it rides the
caller's tenant-scoped session, so RLS applies and **no advisory-lock
domain is needed** (the out-of-process tick pattern of ADR-0093 exists
for *provider I/O*, which materialisation does not perform). The web
"Refresh now" surface is unchanged: it sets the schedule due; the tick
runs the refresh; the refresh's price writes trigger materialisation
in-transaction.

### 4. Write-path re-routing per `valuation_mode`

`InvestmentService.ingest_normalized_series` routes per the target
investment's mode:

- **`nav_price`, unitised investment** → `instrument_prices` via its
  ADR-0092-style conditional `upsert_live` (never `investment_navs`
  directly); materialisation then produces the NAV rows (§2–3).
- **`nav_price`, reported investment** → **refused** with a typed,
  counted skip outcome (`skipped_unit_mismatch`). This replaces the S0
  interim guard's blanket refusal with a mode-aware one; a per-share
  price has no legitimate landing spot in a `reported` book row.
- **Per-share flow kinds (`dividend`, `coupon`), unitised investment**
  → scaled to position level at the routing point:
  `amount = per-share value × holdings(as_of_date)`, then ingested
  into `investment_cashflows` under the unchanged ADR-0092 dedup-key
  idempotency. Zero holdings on the date → counted skip. This closes
  F6.
- **Per-share flow kinds, reported investment** → refused
  (`skipped_unit_mismatch`), same as `nav_price`.
- **Position-level flow kinds** (`distribution`, `capital_call`, `fee`,
  `carry`, `other`) → unchanged behaviour: these arrive
  position-scaled from private-markets-style providers and route as
  today.
- **`weight_*`** → unchanged (`NotImplementedError`, ADR-0092).

`services/investments/market_linked.py` gains the ADR-0097 §9
eligibility clause (`valuation_mode='unitised'`), so
`live_refresh` skips non-unitised investments cleanly before any fetch
— the refusal path in the service is defence in depth, not the primary
filter.

**The S0 interim guard is retired in the same strand** (S3) that lands
this routing: the guard's blanket refusal of per-share kinds is
superseded by the mode-aware routing above, and its tests convert into
the routing's regression suite.

### 5. Regression-test obligations (binding on the strands)

- `reported`-mode investments: end-to-end behaviour **byte-identical**
  to the pre-strand state (Excel transform, manual CRUD, analytics
  outputs on the v29/v30 fixtures).
- Computed writes never mutate `'excel'` / `'manual'` / `'live'` rows
  (row-level guard test per origin, under the unprivileged
  `portfoliflow_app` role).
- Materialisation re-runs are idempotent (value-equal no-op; identical
  final table state; `updated_at` untouched on no-ops).
- Stranded-`'system'`-row deletion never touches any other origin.
- Dividend scaling: per-share × holdings on the ex-date, including the
  zero-holdings skip.
- `tests/regression/test_analytics_layer_pure.py` and
  `test_market_data_layer_pure.py` untouched and passing (holdings
  derivation is pure; materialisation lives outside both layers).

## Consequences

- The P0 (F1) and its sibling (F6) are closed **structurally**: no code
  path can write a per-share magnitude into a position-value column or
  an unscaled per-share flow into the cashflow table.
- Consumers stay untouched; the NAV-series contract (F3) holds. The
  charts, limits, Irene, and reporting see computed NAVs as ordinary
  `actual` rows with `basis='computed'` provenance available for
  UI/audit display.
- The `ingest_origin` value set grows by one value in one migration;
  the audit story ("where did this number come from") extends
  naturally to platform-materialised rows (MaRisk/BAIT posture).
- Excel authority is strengthened: computed rows fill only dates where
  the book of record is silent, and the book's own rows shadow
  computed values wherever both exist.
- The synchronous trigger adds bounded work to transaction/price
  writes; the named successor trigger for moving to an out-of-process
  pattern is a **measured** write-latency problem, not anticipation.

## Alternatives considered

- **Extending `ingest_origin` with `'computed'` instead of
  `'system'`.** Rejected: `computed` is a `basis` word; reusing it as
  an origin value invites exactly the basis/origin conflation the
  standing invariant forbids.
- **Tick-based (out-of-process) materialisation.** Rejected (§3): it
  buys nothing (no provider I/O, no blocking work) and costs
  consistency windows in which prices and NAVs disagree, plus an
  advisory-lock domain and idempotency machinery the in-transaction
  form gets for free.
- **Materialising into a separate `computed_navs` table.** Rejected:
  it breaks the F3 seam — every consumer would need union logic — and
  duplicates the precedence problem instead of reusing the solved
  ADR-0092 row-level guard.
- **Scaling dividends in the adapters instead of at the routing
  point.** Rejected: adapters are DB-free (ADR-0091) and cannot know
  holdings; scaling is an ingest concern where the ledger is in reach.
- **Letting `nav_price` for `reported` listed investments continue to
  land in `investment_navs`.** Rejected: that *is* the P0. There is no
  correct interpretation of a per-share price as a position value.
