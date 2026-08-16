# ADR-0100: Explicit Foreign-Currency Cash Positions and the Redefined Cash Residual

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, cash, fx, currency, limits, aum, engine-contract, phase-8
- **Depends on:** ADR-0099 (conversion machinery)
- **Amends:** ADR-0055 (cash as residual)

---

## Context

ADR-0055 decided to represent cash as the residual of an
authoritative AUM series — `cash(t) = aum_total(t) − Σ nav(t)` —
and explicitly rejected "Option A" (cash as a first-class
``investments`` row), citing the ``investment_type`` CHECK
constraint (seven closed types) and the re-evaluation cost across
every code path that branches on the type (SAA optimiser,
IRR/multiples providers, cashflow engine, audit-and-isolation
tests).

That decision was made under a single-currency premise. With
ADR-0099 introducing the functional-currency model, a gap becomes
visible that the residual cannot express: **cash balances held in
currencies other than the functional currency.** A USD custody
balance is not "what's left after subtracting the invested book" —
it is a position with its own currency, its own valuation history,
its own FX exposure, and (for a Versorgungswerk) its own
AnlV-relevant classification. Folding it into a functional-currency
residual erases exactly the information the multi-currency model
exists to carry.

Two facts have changed since ADR-0055:

1. The asset-class catalogue already contains ``cash`` ("Cash &
   Money Market", `default_asset_classes.json`, sort 120) — the
   classification target exists.
2. ADR-0099 provides point-in-time conversion at a single seam. A
   foreign-currency cash position that is modelled *as an
   investment* flows through that seam with **zero additional
   machinery** — it is converted, aggregated, limit-checked, and
   charted exactly like a fund.

The residual mechanism itself remains institutionally correct for
the functional currency: treasurers know total AUM from custodian
reconciliation, and the operational home-currency float is
genuinely "what's left". The decision is therefore a synthesis, not
a reversal.

## Decision

### 1. `'cash'` becomes the eighth investment type

The ``investments.investment_type`` CHECK is extended by
``'cash'`` (migration). A cash position is an ``investments`` row
with:

- ``investment_type = 'cash'``
- ``asset_class`` → the existing ``cash`` catalogue entry
- ``currency`` = the balance's currency (position currency,
  ADR-0099 concept 2)
- ``valuation_mode = 'reported'`` — the balance is the NAV,
  carried in ``investment_navs`` in position currency, subject to
  the unchanged ADR-0092/0097 currency-equality write rule
- ``vintage_year``, ``commitment_amount`` — NULL (not meaningful)
- ``anlv_code`` — assignable like any investment (cash balances
  are AnlV-classifiable)

**Naming convention (non-normative):** ``Cash USD``, ``Cash GBP``
— one row per (custodian-relevant) currency balance. The
convention aids recognition; identity remains
``(tenant_id, name)`` as everywhere else.

### 2. Which cash balances become explicit rows

Only balances in currencies **other than the tenant's functional
currency** are modelled as explicit cash positions. The
functional-currency float remains the residual (see §3). A tenant
MAY additionally model functional-currency cash explicitly (e.g. a
ring-fenced EUR money-market balance); the residual definition
absorbs this correctly because explicit rows enter Σ NAV.

### 3. The residual is redefined as *cash in functional currency*

ADR-0055's formula is retained but its meaning narrows:

```
residual(t) = aum_total(t) − Σ nav_functional(t)
```

where ``Σ nav_functional`` now sums **all** investments —
including explicit cash positions — after ADR-0099 conversion into
the functional currency. Because explicit cash rows are inside the
sum, the residual automatically shrinks to the unmodelled
home-currency float. No double counting is possible by
construction.

The negative-residual suppression rule (ADR-0055 / ADR-0067 — a
stale AUM row must not surface negative cash) is unchanged.

### 4. Engine and metric contracts for `'cash'`

The type-branch re-evaluation that ADR-0055 priced as Option A's
cost is now paid deliberately, with one rule per engine:

- **NAV aggregation, AUM coverage, limits (Anlagegrenzen):**
  cash positions are **included** — they are part of the invested
  book for coverage and carry their own ``anlv_code``. The
  synthetic residual bucket of the coverage engine keeps its
  existing role for the (now smaller) residual.
- **Performance metrics (IRR / TVPI / DPI, invested-capital
  series, multiples providers):** cash positions are **excluded**.
  The residual never entered these figures under ADR-0055;
  explicit cash rows must not start distorting private-markets
  performance now. The exclusion branches on
  ``investment_type = 'cash'`` at the data-assembly seam
  (ADR-0099 §4), not inside the pure analytics functions.
- **Composition breakdowns:** fund composition and
  currency-exposure views **include** cash; vintage distribution
  **excludes** it (no vintage); region/sector treat it as
  unclassified unless weights are supplied.
- **SAA optimiser:** cash positions map to the ``cash`` asset
  class like any other holding; no optimiser change.
- **Cashflows:** v1 models cash positions as **NAV-only series**
  (interest accrues into the balance). Transfers may optionally be
  recorded with ``flow_type = 'other'``; they never enter
  performance aggregation (excluded per the rule above), so the
  seven-member flow-type set is not extended.

### 5. Out of scope

- **Overdrafts / negative balances** — not modelled; a cash NAV is
  non-negative in v1.
- **Money-market fund look-through** — an MMF is a fund, not a
  cash row; unchanged.
- **FX hedging of cash balances** — deferred with the ADR-0099
  hedging follow-up.
- **Multi-custodian sub-balances per currency** — one row per
  currency suffices in v1; per-custodian splitting is a naming
  convention, not a schema concern.

## Rationale

- The ADR-0055 rejection of Option A rested on two costs: the
  CHECK migration and the branch re-evaluation. The first is one
  migration; the second is now enumerated and decided per engine
  (§4) rather than feared in the abstract — and the multi-currency
  requirement makes paying it unavoidable, because a residual in
  functional currency is *structurally unable* to represent a USD
  balance.
- Modelling FX cash as an investment reuses every existing seam:
  currency-equality writes (ADR-0092/0097), conversion at the
  ADR-0099 boundary, RLS, audit columns, Excel import, limit
  coverage. The alternative (a dedicated ``cash_positions`` table,
  ADR-0055 Option B) would duplicate all of these for no
  expressiveness gain.
- Keeping the residual for the functional-currency float preserves
  the institutional workflow ADR-0055 documented (custodian
  reconciliation as the AUM anchor) and keeps data-maintenance
  duty minimal: tenants model only what actually needs explicit
  treatment.
- Excluding cash from performance metrics keeps IRR/TVPI/DPI
  continuous with their pre-ADR-0100 meaning — no KPI jumps on
  migration day for tenants who add cash rows.

## Alternatives Considered

- **Keep residual-only, convert nothing:** Rejected — cannot
  represent FX cash at all; the motivating gap.
- **Dedicated ``cash_positions`` table (ADR-0055 Option B):**
  Rejected — parallel persistence, parallel import, parallel
  conversion, parallel limit path; everything the investment row
  gets for free.
- **``investment_type = 'other'`` + asset class ``cash`` (no CHECK
  change):** Rejected — the engine contracts in §4 require a
  reliable discriminator; overloading ``'other'`` makes the
  performance-exclusion rule fuzzy and unauditable.
- **Make *all* cash explicit and demote the residual to a
  plausibility check:** Rejected for v1 — forces daily
  home-currency cash NAV maintenance on every tenant, contradicting
  the ADR-0055 finding that treasurers do not maintain such a
  series; revisit if live custodian feeds ever make it free.
- **Extend ``flow_type`` with ``deposit`` / ``withdrawal``:**
  Rejected — cash flows never enter performance aggregation, so the
  distinction buys nothing; ``'other'`` suffices and the closed set
  stays closed.

## Consequences

### Positive

- FX cash balances become first-class: converted, limit-checked,
  AnlV-classified, and visible in exposure tiles with zero new
  machinery.
- The residual regains a crisp meaning (functional-currency float)
  and can no longer silently absorb FX balances.
- Performance metrics are provably unaffected (exclusion rule).

### Negative

- One migration touches a CHECK constraint on the busiest table;
  every ``investment_type`` branch must be audited once against
  the §4 table (the enumerated cost).
- Tenants with FX balances take on NAV maintenance for their cash
  rows (statement-frequency granularity suffices given ADR-0060
  carry-forward).
- The Excel extractor's type-label mapping gains a ``Cash``
  label; workbooks predating it are unaffected but new sample data
  (v31) must exercise it.

### Neutral / Follow-ups

- Front-Office FX-exposure and FX-cash tiles land after this ADR
  (ADR-0067/0072 patterns; Block 5 of the migration plan).
- Glossary v3: *explicit cash position* vs. *cash residual*
  alongside the ADR-0099 currency terms.
- If live custodian balance feeds arrive (ADR-0091 adapter), the
  "all cash explicit" alternative should be revisited.

## Implementation Notes

- Migration ``b027``: extend
  ``ck_investments_investment_type`` by ``'cash'``. No data
  migration; no default rows.
- Data-assembly seam (ADR-0099 §4): filter
  ``investment_type = 'cash'`` out of the performance-metric
  frames; keep it in NAV/coverage/exposure frames. Analytics
  signatures unchanged.
- ``services/data_normalization/investment_extractor.py``: extend
  the type-label mapping (``Cash`` → ``cash``); sample workbook
  v31 adds a ``Cash USD`` column with a short NAV series.
- Limits engine: verify the residual path consumes
  ``Σ nav_functional`` post-conversion; the synthetic
  unallocated/AnlV buckets need no structural change.
- Tests: residual-shrinkage invariant (adding a Cash USD row moves
  value from residual to Σ NAV, total unchanged); performance
  invariance (IRR/TVPI/DPI identical with and without cash rows);
  limit coverage with AnlV-classified cash; extractor round-trip
  for the ``Cash`` label; CHECK-migration up/down.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Functional
  correctness (FX cash representable and non-double-counted),
  Accountability (AnlV classification of cash balances),
  Maintainability (one discriminator, enumerated engine contracts).
- **Audit evidence:** §4 engine-contract table against code
  branches; residual-shrinkage and performance-invariance tests;
  migration b027.
- **Anlagegrenzen relevance:** explicit cash rows carry
  ``anlv_code`` and enter coverage directly — supervisory cash
  treatment becomes attributable instead of residual-implied.

## References

- ADR-0055 (cash as residual — amended by this ADR)
- ADR-0060 (NAV carry-forward — grants statement-frequency
  granularity for cash NAVs)
- ADR-0067 / ADR-0072 (Overview KPI strip and chart row)
- ADR-0092 / ADR-0097 (currency-equality write rule — unchanged
  and load-bearing for cash rows)
- ADR-0099 (functional currency, fx_rates, conversion boundary —
  the machinery this ADR rides on)

---

## Revision History

| Date       | Author                     | Change         |
|------------|----------------------------|----------------|
| 2026-07-10 | PortfoliFLOW project owner | Initial draft. |
| 2026-07-11 | PortfoliFLOW project owner | Accepted against the shipped code. Implemented 2026-07-11: migration `b027` (the `'cash'` eighth investment type), explicit foreign-currency cash positions, and the redefined cash residual (block 4). |
