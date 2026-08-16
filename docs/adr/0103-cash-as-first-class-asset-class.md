# ADR-0103: Cash as a First-Class Asset Class — Unitised Representation, Investor Flows, Plan Path, and Residual Retirement

- **Status:** Accepted
- **Date:** 2026-07-13
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, cash, fx, currency, aum, limits, planning-desk, engine-contract, workbook, phase-8
- **Depends on:** ADR-0097 (position model), ADR-0098 (computed-NAV materialisation), ADR-0099 (functional currency / FX conversion), ADR-0060 (plan/actual cut-over)
- **Amends:** ADR-0100 (explicit FX cash positions and the redefined residual), ADR-0055 (cash as residual — further, beyond ADR-0100's amendment)
- **Honours:** `docs/handover/compatibility-annex-adr-a-adr-c.md` §A and §C
- **Companion:** ADR-0104 (plan-path materialisation and scenario overlay architecture) consumes the cash plan path defined here
- **Interaction basis:** `docs/handover/planning-desk-interaction-decisions.md` (2026-07-13)

---

## Context

ADR-0055 modelled cash as the residual of an authoritative AUM series.
ADR-0100 amended it for the multi-currency world: foreign-currency
balances became explicit `investment_type='cash'` rows
(`valuation_mode='reported'`), while the functional-currency float
remained the residual `aum_total(t) − Σ nav_functional(t)`, and the
option "make all cash explicit and demote the residual" was rejected
*for v1* on one argument: it would force daily home-currency cash-NAV
maintenance on every tenant, contradicting the ADR-0055 finding that
treasurers do not maintain such a series.

The operator decided on 2026-07-07 — reconfirmed as **R1-a** on
2026-07-13 after being presented with the ADR-0100 tension explicitly —
to retire the residual entirely: the same flow-based logic for past,
present, and future; no architectural compromises retained. Three
things have changed since ADR-0100's rejection that make this the
right time rather than a reversal of a fresh decision:

1. **The maintenance argument dissolves at statement frequency.** This
   ADR introduces a workbook **Cash sheet** carrying custodian
   *statement* balances, with ADR-0060 carry-forward covering the gaps
   between statements. Nobody maintains a daily series; the tenant
   types in what the bank statement says, at whatever frequency
   statements arrive. ADR-0100's daily-maintenance cost was priced
   against a representation this ADR does not use.
2. **The Planning Desk needs a flow-constructible cash quantity.** The
   forward cash path is `last actual balance + Σ signed plan flows +
   investor flows` (decision D2). Projecting a quantity the book
   defines residually would mean the plan world and the book disagree
   about what cash *is* — precisely the kind of conceptual debt R1-a
   removes.
3. **The position model exists.** ADR-0097 §6 already reserves the
   unitised flip for cash ("cash joins via the future ADR-A"), and the
   compatibility annex §A.1 pins the degenerate-unitised
   representation this ADR now realises.

One verification finding from 2026-07-13 also lands here: the
efficient-frontier assembly (`portfolio_analysis_service.py`) loads
active investments **unfiltered** — a cash row with two NAV
observations would today be optimised as a frontier asset. ADR-0100 §4
said "no optimiser change", which is not the exclusion statement; this
ADR pins it.

## Decision

### 1. Cash is the degenerate unitised case — with stored unity prices

Every cash position (functional-currency and foreign-currency alike)
is an `investments` row with:

- `investment_type = 'cash'` (unchanged from ADR-0100 §1)
- `valuation_mode = 'unitised'`
- units ≡ currency units: **balance = holdings**, derived purely from
  `position_transactions` per ADR-0097 §4
- **stored** `instrument_prices` rows of exactly `1.0000`, one per
  statement date, in the position currency (satisfying the ADR-0097 §5
  currency-equality rule)

**Why stored, not implied.** The ADR-0098 materialised set is defined
as one NAV row *per `instrument_prices` date* on or after the first
ledger date. An implied constant price would give a cash position at
most zero materialisable dates and would force a special case into the
materialisation service — exactly what annex §A.1 forbids ("must work
without special-casing consumers"). With one unity price row per
statement date, the unchanged ADR-0098 service materialises
`holdings(date) × 1.0000 = statement balance` as an ordinary
`nav_kind='actual'`, `basis='computed'`, `ingest_origin='system'` row.
The statement date is simultaneously a units observation and a price
observation; the book machinery needs no new branch.

ADR-0097 §6's flip-eligibility precondition
(`listed_equity`/`listed_bonds`) is extended by `'cash'`. The flip
remains one-way and the live-ingest eligibility of cash remains
**denied** (ADR-0097/ADR-0092 lineage: `cash` is excluded from live
ingest regardless of mode; unity prices come only from the import path
defined here).

### 2. All cash becomes explicit; the residual retires

The functional-currency float ceases to be a residual. Each tenant
models its functional-currency cash as an explicit cash position
(naming convention `Cash EUR` etc., non-normative), fed by the Cash
sheet (§3). The redefined residual of ADR-0100 §3 — and with it the
formula `aum_total − Σ nav_functional` — is **retired**. AUM is
defined uniformly as

```
aum(t) = Σ nav_functional(t)        (all investments, incl. cash rows)
```

for past, present (actuals), and future (plan world, ADR-0104). There
is no unmodelled float: what is not on a statement does not exist for
the platform. The negative-residual suppression rule (ADR-0055/0067)
retires with the residual; its concern (stale AUM rows) has no
equivalent, because there is no independent AUM series left to go
stale.

Ordering constraint (annex §A.3, binding on implementation): the
`portfolio_aum` retirement (§7) executes **after** cash rows
materialise correctly and reconcile once — within the same
implementation strand, but as its final step, never before.

### 3. The Cash sheet — workbook format delta (v32)

The Excel workbook gains a **`Cash` sheet** (statement-style, the
book-of-record source for cash balances):

- One column per cash position (matching the investment-column
  convention of the NAV sheets; the `Cash USD` column of v31 moves
  here), one row per **statement date**, cell = balance in position
  currency.
- The functional-currency cash column is expected for every tenant
  from v32 on; foreign-currency columns as needed.
- Import creates missing cash-position rows (type `cash`, asset class
  `cash`, `valuation_mode='unitised'`) exactly as the investment
  extractor does for other sheets, subject to the unchanged ADR-0092
  Excel-precedence and conditional-upsert rules.

The existing **`AUM` sheet is demoted to an optional reconciliation
input**: when present, the importer compares each stated AUM value
against `Σ nav_functional` on that date and emits an **import
warning** on deviation beyond the `Numeric(20, 4)` quantum — nothing
is persisted. This preserves the ADR-0055 institutional finding
(custodian reconciliation as the treasurer's anchor) as a *control*
rather than as a parallel data model. A workbook without the sheet
imports without warnings.

### 4. Statement-to-ledger derivation

A statement balance is a level; the ledger stores flows. The Cash
sheet import derives, per cash position and statement date, in order:

1. **First statement date** → one `txn_type='opening'` transaction,
   `units = balance`, `ingest_origin='excel'` (mirroring the ADR-0097
   §7 units-row synthesis).
2. **Every subsequent statement date** → one `txn_type='transfer'`
   transaction with `units = balance(date) − balance(previous
   statement date)`, signed, `price_per_unit = NULL`,
   `consideration = NULL`. A zero delta writes nothing.
3. **Every statement date** (including the first) → one
   `instrument_prices` row of `1.0000` (§1).

Interest, fees, and sweeps need no separate modelling: the statement
balance already contains them, and the delta transfer carries them
implicitly. Re-import idempotency follows the existing classify-
then-write idiom: an unchanged sheet produces byte-identical ledger
and price state. Between statements, consumers see the last
materialised balance via the ordinary ADR-0060 carry-forward — the
same convention as every other NAV series.

Actual balances remain non-negative (ADR-0100 §5 unchanged); the
importer rejects a negative statement cell. Negative values are legal
only in the **plan** path (§6), where they are the funding-need
signal, not an error.

### 5. Investor flows — the eighth `flow_type`

The `investment_cashflows.flow_type` CHECK is extended (CHECK-swap
migration, annex §C additive-extension rule) by **`'investor_flow'`**:
net contributions to / withdrawals from the mandate.

- **Booked on cash positions only.** An investor flow is a row on the
  cash position of the currency it settles in (decision N4);
  multi-currency tenants may hold investor flows per currency. A
  validation rule rejects `investor_flow` rows on non-cash
  investments.
- **Signed, both `flow_kind` variants.** `plan` investor flows enter
  the cash plan path (§6); `actual` investor flows are informational
  (reporting, net-flow analytics) — they never drive the actual
  balance, which comes exclusively from statement levels (§4). No
  double counting is possible by construction: levels come from
  statements, flows never materialise actual NAVs.
- **Provenance, RLS, Excel import, dedup unchanged.** The
  rule-based dedup key already composes over `flow_type`; the new
  member participates without mechanical change. The workbook carries
  plan investor flows in the existing plan-flow sheet convention
  (exact label fixed in the implementation strand against the
  extractor's conventions).
- **Exemption invariant (binding, regression-testable):** no scenario
  transformation and no TA transformation ever creates, deletes,
  re-paces, or re-scales an investor flow. ADR-0104 owes the
  enforcement in the overlay executors; this ADR owes the invariant's
  definition and its regression test at the flow-type level.
- **Performance exclusion:** `investor_flow` rows never enter
  IRR/TVPI/DPI or the ADR-0066 cashflow adjustment — they ride on
  cash positions, which are performance-excluded wholesale (ADR-0100
  §4, reaffirmed in §8 below), so no per-flow-type branch is needed.

### 6. The cash plan path — a materialised computation

Per cash position, the forward path

```
cash_plan(d) = balance(t₀) + Σ_{t₀ < t ≤ d} signed plan flows(t)
                            + Σ_{t₀ < t ≤ d} plan investor flows(t)
```

with `t₀` = the last actual statement date and *plan flows* = the
signed plan-kind flows of **all** investments settling in that cash
position's currency (settle-against-cash, decision ex-D6: a plan
capital call debits cash, a plan distribution credits it), is
**materialised** as `investment_navs` rows on the cash position:
`nav_kind='plan'`, `basis='computed'`, `ingest_origin='system'`,
`source='computed:cash-plan'`, one row per flow-event date after
`t₀`.

- A **new, separate service** (`services/investments/`
  neighbourhood, working name `cash_plan_materialisation`) owns this.
  The ADR-0098 service is **not extended** — it materialises actuals
  from `holdings × price` and pins `nav_kind='actual'`; the plan path
  is a different computation with a different anchor. Both share the
  classify-then-write idempotency idiom, the `'system'`-rows-only
  mutation rule, and stranded-row deletion.
- **Triggers** (synchronous, in-transaction, ADR-0098 §3 pattern):
  Cash-sheet import (a new `t₀` moves the anchor), plan-flow import,
  investor-flow import or edit. Callers pass `since` where they know
  the earliest affected date.
- **Plan rows stay value-based** (annex §B.1): the materialised rows
  are position values; there is no plan-units decomposition and no
  plan-side ledger write.
- Plan balances **may be negative** — a projected funding gap is the
  single most decision-relevant signal the Planning Desk shows.
- Multi-currency (decision N2): the path materialises **per currency**
  in position currency; conversion into the functional currency
  happens at the ordinary ADR-0099 §4 seam, under the plan-world FX
  convention pinned by ADR-0104 (decision N1: carry-forward of the
  last actual rate, held flat).

### 7. `portfolio_aum` retires; consequence inventory

A forward migration (number claimed at implementation time) drops the
`portfolio_aum` table; the model, the repository, and every consumer
switch to `aum(t) = Σ nav_functional(t)`. The verified consumer
inventory (Repomix 2026-07-12; 39 files) that the implementation
strand must work through:

- **Engines:** `services/analytics/limit_coverage.py` — the
  `aum_series` input, the `cash_residual` output, and the synthetic
  residual bucket retire; the AnlV/SAA denominator becomes Σ NAV
  (which now *contains* cash rows). `services/limits/
  limits_coverage_service.py` assembles accordingly.
- **Services:** `services/front_office_overview/overview_service.py`
  (AUM tile → Σ NAV), `services/irene/internal_delta.py` (delta
  baseline → Σ NAV), `services/investments/investment_service.py`
  (transform/import path stops persisting AUM).
- **Web:** `web/routes/limits.py`, `web/routes/overview.py`,
  `web/routes/data_import.py` (AUM sheet handling → §3 reconciliation
  warning).
- **Core:** `core/models/portfolio_aum.py`,
  `core/repositories/portfolio_aum_repository.py`, both `__init__`
  exports.
- **Tests:** ~15 suites (fixtures in `tests/_db_fixtures.py` and
  `tests/services/irene/_book_fixtures.py` included) rewritten against
  Σ NAV; the ADR-0102 zero-read FX spy proofs must keep passing
  unchanged for single-currency tenants.
- **Docs:** `CLAUDE.md`, `docs/architecture.md`, roadmap.

`SeriesKind`/capability surfaces are untouched (live FX remains #042,
out of scope).

### 8. Engine and metric contracts — delta to ADR-0100 §4

ADR-0100 §4 remains in force with these changes:

- **Efficient frontier / CML (new, pinned):** cash positions are
  **excluded from the frontier universe** at the data-assembly seam
  (`portfolio_analysis_service`, filter `investment_type != 'cash'`
  before NAV loading — never inside pure analytics, ADR-0013/0045).
  Cash is the risk-free anchor of the capital market line, represented
  by the `risk_free_rate` parameter; it is never a frontier asset.
  The same seam-level exclusion rule extends to any future
  optimiser-adjacent assembly.
- **Coverage/limits:** cash rows remain **included** (unchanged); the
  synthetic residual bucket retires with the residual (§7).
- **Performance metrics:** cash exclusion unchanged
  (ADR-0100 §4); `investor_flow` adds nothing to exclude (§5).
- **Composition:** unchanged (currency exposure includes cash;
  vintage excludes it).
- **Cashflows:** ADR-0100 §4's "NAV-only, transfers optionally
  `'other'`" is superseded for cash by §4/§5 of this ADR: balances
  flow through the ledger; `investment_cashflows` on cash positions
  carries investor flows (and remains open to `'other'` rows, which
  stay performance-inert).

### 9. Migration path for ADR-0100 rows (decision N5)

Existing `investment_type='cash'` rows (`reported`, NAV-fed) convert
by a data migration in the implementation strand:

1. Per cash row, read its actual NAV history (position currency).
2. Synthesise the ledger: `opening` at the earliest NAV date
   (`units = nav_value`), one signed `transfer` per subsequent NAV
   date (delta), `ingest_origin='excel'` provenance mirrored from the
   source rows.
3. Write one `1.0000` price row per NAV date.
4. Flip `valuation_mode` to `'unitised'` (the one-way direction
   ADR-0097 §6 reserves for this ADR).
5. Run the ADR-0098 materialisation: existing `'excel'`/`'manual'` NAV
   rows are precedence-protected and value-identical by construction
   (`balance × 1.0000`); the run is a provable no-op on them, which is
   the migration's acceptance check.

### 10. Out of scope

- Overdrafts / negative **actual** balances (unchanged, ADR-0100 §5).
- FX hedging of cash balances (ADR-0099 follow-up).
- Live FX supply (#042) and live ingest for cash (permanently
  ineligible, §1).
- Money-market fund look-through (an MMF is a fund; unchanged).
- Multi-custodian sub-balances per currency (naming convention, not
  schema).
- The overlay architecture, plan-world FX convention, and scenario
  semantics (ADR-0104).
- TA-generated pacing profiles (#023, future ADR-D).

## Rationale

- **The residual was a modelling debt with an expiry date, and the
  date arrived.** ADR-0100 kept it because the alternative was priced
  at daily maintenance. At statement frequency with carry-forward,
  the price is "type what the statement says" — strictly less work
  than maintaining the AUM sheet the residual itself required. The
  ADR-0055 treasurer finding is preserved where it belongs: as the
  §3 reconciliation control.
- **One cash model for past and future.** The Planning Desk projects
  `balance + flows`. If the book defined cash any other way, baseline
  and book would be two theories of the same quantity. R1-a makes the
  book flow-based too, and "hypothetical transactions never change
  AUM" becomes a construction property (`Σ NAV_plan + cash_plan`)
  rather than a checked condition.
- **Stored unity prices buy zero-special-case reuse.** One redundant-
  looking price row per statement date is the entire cost of running
  cash through the unchanged ADR-0097/0098 machinery — ledger,
  holdings derivation, materialisation, precedence, provenance, RLS —
  with no new branch anywhere in the book path (annex §A.1 satisfied
  literally).
- **Statement levels drive balances; flows never do.** Keeping
  `actual` investor flows informational avoids the classic
  double-count between a statement that already contains a
  contribution and the contribution row itself. The plan world, which
  has no statements, is exactly where flows *do* drive the path.
- **The frontier exclusion closes a real hole now, cheaply.** Today
  the exposure is theoretical (reported cash rows rarely carry ≥2
  NAVs); after this ADR every cash row materialises a dense NAV
  series, and an unfiltered frontier would happily optimise a
  zero-variance pseudo-asset. Pinning the seam filter in the same ADR
  that creates the dense series is the honest sequencing.

## Alternatives Considered

- **R1-b (staged retirement behind a named trigger):** Rejected by
  the operator (2026-07-13). It leaves `portfolio_aum` load-bearing
  while the Planning Desk projects a flow-based quantity the book
  defines residually — a live contradiction, and named-trigger debt
  violates the no-structural-debt principle.
- **Keep the ADR-0100 synthesis (residual for the functional
  float):** Rejected — R1-a; see Rationale. The maintenance argument
  no longer holds at statement frequency.
- **Implied unity price (no `instrument_prices` rows):** Rejected —
  forces a cash special case into the ADR-0098 materialisation or its
  consumers; violates annex §A.1. Stored rows cost one row per
  statement date.
- **Keep cash `reported` and write balances as NAVs directly:**
  Rejected — no ledger means no flow-based construction, no
  transaction-level provenance for balance changes, and a permanent
  representation split between cash (level-fed) and the plan world
  (flow-fed).
- **Drop the AUM sheet entirely:** Rejected in favour of the §3
  reconciliation demotion — the custodian-reconciliation workflow is
  an institutional control worth keeping; persisting it is the part
  that was debt.
- **Extend the ADR-0098 service to also write plan rows:** Rejected —
  ADR-0098 §2 pins `nav_kind='actual'` and its semantics are an
  accepted, immutable contract; a separate service with a shared
  idiom keeps both auditable (and annex §B.4's reusable-not-extendable
  rule applies in spirit to the book path generally).
- **`investor_flow` as transactions instead of cashflows:** Rejected —
  investor flows are analytically flows (reporting, plan path), not
  units events; the ledger would conflate mandate-level flows with
  position mechanics, and annex §A.2 explicitly routes them through
  `investment_cashflows`.

## Consequences

### Positive

- One uniform cash model: balance = holdings, past = statements,
  future = flows; AUM ≡ Σ NAV everywhere with no residual, no
  suppression rule, no synthetic coverage bucket.
- The Planning Desk baseline (ADR-0104) reads ordinary NAV series —
  actual and plan — through unchanged contracts.
- FX cash, functional cash, and every other investment flow through
  identical machinery; the ADR-0100 migration path (§9) is provably
  value-neutral.
- The frontier hole is closed before it can materialise.

### Negative / cost

- The widest consequence inventory of the programme so far (§7, 39
  files) — one strand, but a large one; recommended as a separate
  implementation chat per the rollout plan.
- Tenants must supply a Cash sheet from v32 on (functional-currency
  column expected); the workbook grows a sheet while the AUM sheet
  demotes.
- Unity price rows add ~one row per cash position per statement date
  (negligible volume, nonzero conceptual noise in the prices table —
  priced against the zero-special-case gain).
- The ADR-0102 attribution follow-ups (#045 lineage) and any consumer
  that assumed "AUM is an imported series" must be re-audited once
  (covered by the §7 inventory).

### Operator action required

- Accept this ADR (and ADR-0104) before any implementation prompt is
  produced (Phase 4 gate).
- Produce workbook v32 (Cash sheet; `Cash USD` column moves; AUM sheet
  optional) against the format delta fixed in the implementation
  strand.
- Run one reconciliation cycle (imported statement balances vs. the
  retiring residual) before the `portfolio_aum` drop executes —
  sequenced inside the strand, surfaced in its report.

---

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-07-13 | PortfoliFLOW project owner | Proposed. Cash unified as a first-class, unitised asset class: workbook Cash sheet (v32), `flow_type='investor_flow'`, materialised cash plan path, frontier cash exclusion pinned at the assembly seam, `portfolio_aum` retired by forward migration. Amends ADR-0100 and ADR-0055. |
| 2026-07-13 | PortfoliFLOW project owner | Accepted ahead of implementation (the ADR-0090–0093 design-first precedent — the Phase-4 gate this ADR's §Operator action names). Registered in `docs/adr/README.md`; implementation tracked as roadmap **#048**, migrations claimed at implementation time. No code has shipped against it yet. |
