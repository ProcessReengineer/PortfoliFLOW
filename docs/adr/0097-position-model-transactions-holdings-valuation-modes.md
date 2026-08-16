# ADR-0097: Position Model — Transaction Ledger, Holdings Derivation, Valuation Modes, and Instrument Prices

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** PortfoliFLOW project owner
- **Supersedes / amends:** redeems the `basis='computed'` reservation of ADR-0079 (which remains authoritative for return conventions); extends the investment domain of ADR-0027/ADR-0090; interacts with ADR-0092 (whose upsert semantics remain unchanged)
- **Implements roadmap item:** #038 — Position Model (Transactions, Holdings, and Unitised Valuation)
- **Tags:** schema, position-model, unitised-valuation, transactions, market-data, rls, audit

---

## Context

PortfoliFLOW's book is historically **NAV-driven**: a position is a euro
amount (`investment_navs.nav_value`) on a statement day, never a quantity
of units at a per-unit price. That was correct for the original
fund-of-funds target group — a private-markets fund stake has no natural
unit count. With ADR-0074 the product scope widened to general
professional investors, and the platform is acquiring capabilities that
structurally require a **transaction-driven, unitised position model**
for listed instruments, as every whole-book institutional system
(SimCorp, Aladdin, Bloomberg AIM, Addepar) uses: live market-data ingest
(#036), the Planning Desk with hypothetical transactions, scenario
analysis (#034), and a future execution layer.

The forcing incident is a **latent P0** verified by code inspection on
2026-07-08 (finding F1): `SeriesKind.NAV_PRICE` is documented and routed
as "landing in `investment_navs.nav_value`". The Yahoo adapter maps the
**unadjusted per-share EOD close** and the Bloomberg adapter maps daily
`PX_LAST` — per-share magnitudes — while the Excel book of record stores
**position values** in the same column. The ADR-0092 Excel-precedence
guard prevents *overwrites* of `'excel'` rows but **inserts** live rows
on dates where the book of record is silent, producing NAV series that
jump between millions and hundreds. A sibling finding (F6, 2026-07-08)
shows the same unit mismatch for `dividend`: Yahoo dividend events are
per-share amounts, ingested unscaled into position-level
`investment_cashflows`. Live series ingest for listed instruments is
therefore unsafe until the write path is re-routed (ADR-0098) — an
interim guard ships independently (strand S0).

Load-bearing facts, verified in the snapshot (findings F2–F5):

- **F2** — The seam is already reserved: `investment_navs.basis`
  (`'reported' | 'computed'`, ADR-0079) defines `'computed'` as "a future
  holdings-aggregation result", and ADR-0092 explicitly defers
  "Purchases/sales of positions (changing holdings)". This ADR redeems
  that reservation; it reinterprets nothing.
- **F3** — The blast radius is write-side. `nav_value` appears in ~60
  files, but analytics (ADR-0013, pure), chart specs, statistics, limits,
  Irene, and reporting all consume **NAV series passed in as pandas
  objects** and are indifferent to whether a series is reported or
  materialised from units × price.
  `compute_cashflow_adjusted_return_series` (ADR-0066) is scale-invariant
  and flow-correct.
- **F4** — The Excel book of record (format ADR-0009, test data v29)
  carries **no unit counts**; NAV sheets are position values.
- **F5** — `nav_kind='plan'` rows plan future position **values**;
  decomposing plans into plan-units × plan-prices would be artificial.

## Decision

A **three-layer position model**: sources (a transaction ledger and a
per-unit price series), a materialisation service (ADR-0098), and
unchanged consumers on the NAV-series contract. This ADR fixes the
**schema and semantics** of the source layer and the valuation-mode
discriminator. ADR-0098 fixes materialisation and write-path re-routing.

### 1. `investments.valuation_mode`

A new column on `investments`:

```
valuation_mode TEXT NOT NULL DEFAULT 'reported'
CHECK (valuation_mode IN ('reported', 'unitised'))
```

- `'reported'` — NAV is carried directly in `investment_navs`, exactly
  as today. The default for the four private types (`private_equity`,
  `private_debt`, `real_estate`, `infra_equity`) and `other`, and the
  **backfill value for every existing row of every type** (§6).
- `'unitised'` — NAV is materialised from holdings × price
  (ADR-0098). The **target** mode for `listed_equity` and
  `listed_bonds`, and — per the pinned interface constraint for the
  future cash ADR (ADR-A) — for cash, as the degenerate case
  `price ≡ 1.0000`, balance = holdings.

The mode is a **per-investment operational fact, not a per-type
automatism**: a listed instrument without unit information stays
`'reported'` (§6). The mode governs write-path routing only; no reader
of NAV series may branch on it (F3 is the protected seam).

### 2. `position_transactions` — the transaction ledger

One row per position-changing event. Holdings follow deterministically
from the ledger (§4); the ledger is the single source of truth for unit
counts.

```
position_transactions (
    id              UUID PK DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    investment_id   UUID NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
    txn_type        TEXT NOT NULL
                    CHECK (txn_type IN ('opening','buy','sell','transfer')),
    trade_date      DATE NOT NULL,                    -- statement-day semantics
    units           NUMERIC(24, 8) NOT NULL,          -- signed; see sign rules
    price_per_unit  NUMERIC(20, 8),                   -- nullable; see rules
    consideration   NUMERIC(20, 4),                   -- signed cash effect, optional
    currency        TEXT NOT NULL,                    -- must equal investments.currency (§5)
    note            TEXT,
    source          TEXT,                             -- free-text provenance
    ingest_origin   TEXT NOT NULL
                    CHECK (ingest_origin IN ('excel','live','manual')),
    created_by      UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

Constraints and conventions:

- **Sign rules (CHECK-enforced):** `opening`/`buy` require `units > 0`;
  `sell` requires `units < 0`; `transfer` requires `units <> 0`.
  Signed quantities keep holdings derivation a plain cumulative sum.
- **Price rules:** `price_per_unit`, when present, must be `> 0`.
  `buy`/`sell` require a price; `opening`/`transfer` may omit it (an
  Excel-synthesised opening or an in-kind transfer has no trade price).
- **At most one `opening` per investment:** partial unique index
  `uq_position_transactions_opening ON (investment_id) WHERE
  txn_type = 'opening'`. The opening anchors the ledger; corrections
  edit the opening row rather than stacking a second one.
- **`ingest_origin`** uses the uniform ADR-0092 triple for consistency
  across the write-path families, even though no `'live'` transaction
  writer exists yet (ADR-0092 deferred it; a future execution layer is
  the named consumer). Excel-synthesised openings (§7) carry
  `'excel'`; web-entered transactions carry `'manual'`.
- **No SQL enums** — TEXT + CHECK, per the codebase convention
  (b019/b020 precedent). **RLS** via `apply_tenant_rls`, tested under
  the unprivileged `portfoliflow_app` role.
- **Determinism tiebreak:** derivation order is
  `(trade_date, created_at, id)` — total and reproducible.

Distributions and dividends remain **cashflows, never unit
operations** (`investment_cashflows`); the price drops ex-distribution —
the ADR-0079 pinned distribution-attachment convention carries over
unchanged.

### 3. `instrument_prices` — the per-unit price series

```
instrument_prices (
    id              UUID PK DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    investment_id   UUID NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
    as_of_date      DATE NOT NULL,                    -- statement day, mirrors investment_navs
    price           NUMERIC(20, 8) NOT NULL CHECK (price > 0),
    currency        TEXT NOT NULL,                    -- must equal investments.currency (§5)
    source          TEXT,                             -- provider name / free text
    ingest_origin   TEXT NOT NULL
                    CHECK (ingest_origin IN ('excel','live','manual')),
    created_by      UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (investment_id, as_of_date)                -- uq_instrument_prices_investment_date
)
```

- **Keyed per investment, not per security-master instrument** — §Design
  question 1 below. The keying deliberately mirrors `investment_navs`
  (`(investment_id, as_of_date)`; no kind dimension — prices are
  actuals; plan/scenario price paths are ADR-C workspace concerns and
  never live here).
- **One canonical price, no `price_kind`** (close/bid/ask) — §Design
  question 3. The pinned basis is the provider's daily valuation price:
  Yahoo **unadjusted EOD close**, Bloomberg **`PX_LAST`** — exactly what
  the adapters already normalise (ADR-0091). Bid/ask/mid is a named
  successor concern with no current consumer (YAGNI).
- The repository gains an `upsert_live` **value-identical in guard
  semantics to ADR-0092**: `INSERT ... ON CONFLICT
  (investment_id, as_of_date) DO UPDATE ... WHERE existing
  ingest_origin = 'live'` — a live price refreshes only its own prior
  rows and never mutates `'excel'`/`'manual'` price rows.

### 4. Holdings: derived on read, no snapshot table

Holdings (units per statement day) are a **pure derivation** over the
ledger — the cumulative signed sum of `units` ordered by the §2
tiebreak, evaluated as a step function of `trade_date`:

- Implemented as a pure, DB-free module
  `services/investments/holdings.py` (mirroring the pure-predicate
  precedent of `services/investments/market_linked.py`). It is **not**
  placed under `services/analytics/` — its concern is position
  bookkeeping, not analytics — but it observes the same purity: no DB,
  no FastAPI, no provider imports.
- **No `holdings` snapshot table** (§Design question 2). Rationale: the
  ledger for an institutional book of this shape is tiny (tens of
  positions, transactions are rare events); derivation is a cumulative
  sum; and the **materialised computed-NAV rows in `investment_navs`
  are already the persisted product** that charts and analytics query.
  A snapshot table would add an invalidation discipline and an RLS
  surface for no measurable read-path gain. Named successor trigger:
  ledger cardinality or query profiles that make on-read derivation a
  measured bottleneck.
- **Non-negativity invariant:** no transaction may take derived
  holdings below zero on any date. Enforced at write time in the
  service layer (recompute forward from the affected date; reject the
  write with a domain error). Short positions are out of scope; a
  successor ADR owns them if ever needed.

### 5. FX conventions — one rule, no silent conversion

`instrument_prices.currency` and `position_transactions.currency` must
equal `investments.currency`. The write paths **reject** (typed skip
with a counted outcome on ingest; validation error on manual entry) a
price or transaction in any other currency rather than converting.
Materialised NAV currency = investment currency, byte-compatible with
today's `investment_navs.currency` semantics; the single conversion
point to report currency stays exactly where it is today, downstream of
the NAV-series contract. Cross-currency instruments (e.g. a USD listing
in a EUR book) are a **named successor concern** — the conservative
rule prevents silent FX errors until that ADR exists.

### 6. Backfill and migration policy for existing tenants

- The `valuation_mode` column lands with `DEFAULT 'reported'` and
  **every existing investment of every type backfills to
  `'reported'`**. No automatic flips — historical `reported` rows are
  untouched, behaviour is byte-identical post-migration.
- Flipping an investment to `'unitised'` is an **explicit operator
  act** with preconditions: the investment has at least an `opening`
  transaction (from the Excel units row, §7, or web entry), and its
  type is `listed_equity`/`listed_bonds` (cash joins via the future
  ADR-A). The flip is **one-way**; corrections go through ledger edits,
  never a mode flap.
- **On flip, all `ingest_origin='live'` rows in `investment_navs` for
  that investment are deleted.** They are per-share prices written into
  a position-value column — the F1 defect artifacts — and their
  information content is re-ingested correctly into
  `instrument_prices`. `'excel'` and `'manual'` NAV rows are never
  touched.
- Investments with identifiers but **no unit information stay
  `'reported'` and live-series-ineligible** until an operator supplies
  units (the §Design-question-8 answer, harmonised with §7: no units →
  no unitisation → no live series ingest).

### 7. Excel import: the units row (format v30)

The import format (ADR-0009 lineage) gains **two optional Attributes
rows** (exact labels fixed in the S4 strand against the extractor's
label conventions):

- `Units` — the unit count, a positive decimal.
- `Units As Of` — the date the count refers to; **defaults to the
  investment's earliest actual NAV date** when absent.

At import, a present units row is synthesised into **one `opening`
transaction** (`ingest_origin='excel'`, `trade_date = units-as-of
date`, `price_per_unit = NULL`; the day-one price follows from
NAV ÷ units at materialisation time, ADR-0098). The units row is the
**authoritative default path**: actual custodian unit counts beat any
derived count. **No value÷price synthesis** is performed — deriving
units from position value and a provider price bakes rounding and
price-basis mismatches into the ledger (§Design question 5: format
extension is the default; synthesis-from-values is rejected). Re-import
follows Excel book-of-record semantics: the `'excel'` opening row is
reconciled (updated in place), never duplicated — the partial unique
index makes duplication structurally impossible. Test data becomes
**v30** with units for the listed instruments.

### 8. Synthetic unitisation of private-markets positions — specified, deferred

Pension-fund-style unitisation of private positions is **specified here
and not implemented now**. The pinned mechanics for the future
implementation:

- Acquisition unitises **at par**: a first drawdown/purchase of X EUR
  mints X units at price 1.0000.
- Running unit price = reported position NAV ÷ outstanding units; the
  **price carries the performance**, units are constant between
  transactions.
- Later capital calls / commitment increases mint new units **at the
  prevailing unit price** (a call of Y EUR at price p mints Y/p
  units) — never at par, or the price series gets a fictitious
  valuation jump.
- Partial sales / secondaries of Z EUR burn Z/p units; proceeds are a
  cashflow.
- Distributions remain cashflows, never unit operations (price drops
  ex-distribution — ADR-0079).

**Implementation trigger (outlook):** the first real tenant use case of
a partial sale / secondary or commitment restructuring that
reported-NAV + cashflow-adjusted returns (ADR-0066) cannot represent
adequately. Until then, PM analytics need no units: IRR, TVPI/DPI/RVPI
and NCG are unit-blind, and time-weighted returns over flows are
handled by ADR-0066.

### 9. Market-linked predicate extension

`services/investments/market_linked.py` gains `valuation_mode`
awareness: an investment is **live-series-eligible** iff (1) its type is
market-linked, (2) it carries a primary market-usable identifier
(ADR-0090), **and (3) `valuation_mode = 'unitised'`**. Clause (3) is
enforced in strand S3 together with the ADR-0098 re-routing (until
then, the S0 interim guard blocks per-share kinds globally). This
clause is additive and orthogonal to the deferred private-markets
predicate generalisation noted in ADR-0096 §3, which keeps its own
trigger.

## Design questions resolved (recorded per handover §6)

1. **Instrument vs. position separation:** later. `instrument_prices`
   is keyed per investment, mirroring `investment_navs`. A
   security-master (instruments as first-class entities, shared price
   series) is a **named successor concern**; trigger: the first case
   where two investments or tenants must share one instrument's series,
   or a security-reference domain (names, classifications, corporate
   actions) is needed.
2. **Holdings:** derived on read; no snapshot table (§4).
3. **Price kinds:** single canonical price; no `price_kind` (§3).
4. **Materialisation trigger:** synchronous in-transaction — decided
   and specified in ADR-0098.
5. **Excel import:** units-row format extension is the default and only
   path; value-derived synthesis rejected (§7).
6. **Plan-side contract:** plan rows stay value-based — pinned in §Consequences
   and the compatibility annex (constraint for the future ADR-C).
7. **ADR-A interface constraint:** cash = `valuation_mode='unitised'`,
   `price ≡ 1.0000` — pinned in the compatibility annex.
8. **Backfill policy:** §6.
9. **Predicate interaction:** §9.

## Consequences

- The write paths change; **no reader changes**. Analytics, chart
  specs, statistics, limits, Irene, and reporting keep consuming NAV
  series unchanged — the F3 seam is the load-bearing property that
  bounds the migration.
- `reported`-mode investments behave **byte-identically** to the
  pre-ADR state; regression evidence is a strand obligation.
- `nav_kind='plan'` series remain value-based; nothing in this ADR or
  its successors decomposes plans into plan-units × plan-prices. The
  Planning Desk's hypothetical transactions will operate on the ledger
  in ADR-C workspace structures and never write book rows.
- Two new tenant-scoped tables and one column arrive in **one
  migration (`b024` or the then-current next free slot)**; the
  `ingest_origin` extension needed by materialisation is ADR-0098's
  migration, keeping one concern per migration.
- The Excel workbook remains the book of record; its authority is
  untouched and extended (the units row is `'excel'`-origin data with
  full precedence).
- Live series ingest for listed instruments remains blocked (S0 guard)
  until ADR-0098's re-routing lands — the P0 stays closed throughout.

## Alternatives considered

- **Positions as first-class rows with a mutable `current_units`
  column.** Rejected: mutable aggregates without an event trail cannot
  answer "holdings as of date d", break auditability (MaRisk/BAIT
  posture), and make backdated corrections destructive. The ledger is
  the institutional-standard representation.
- **A `holdings` snapshot table.** Rejected for now (§4); named
  successor trigger recorded.
- **Deriving units at import from position value ÷ provider price.**
  Rejected (§7): bakes rounding and price-basis mismatches into the
  ledger; custodian unit counts are the accurate source.
- **Automatic mode flip for all listed instruments at migration.**
  Rejected: without unit data the flip is meaningless, and a silent
  behavioural change to existing tenants violates the byte-identical
  backfill principle.
- **A generic `instrument` entity now (security master).** Rejected as
  premature (§Design question 1); the per-investment keying converts
  cleanly later because `instrument_prices` already isolates the price
  concern in one table.
- **Allowing cross-currency prices with inline FX conversion.**
  Rejected (§5): a silent conversion point inside the write path is an
  audit hazard; the conservative equality rule fails loudly instead.
