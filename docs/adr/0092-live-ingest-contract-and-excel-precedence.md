# ADR-0092: Live-Ingest Contract and Excel Precedence — A Typed Ingest-Origin Field and a Conditional Upsert That Never Overwrites Excel

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Live Data Import (provider-agnostic ingest)
- **Tags:** market-data, data-import, schema, data-integrity, provenance, audit

---

## Context

Live import augments the data basis of existing investments by writing
into the **same target tables** the Excel extractor already writes:
`investment_navs`, `investment_cashflows`, and the historised composition
weights (ADR-0079 / ADR-0080). It is a **second producer** into an
existing contract, not a new data model.

This raises a collision question the Excel-only world never had. When a
live fetch produces a value for a `(investment, date, kind)` that Excel
has already supplied, **which wins?**

The answer is a business invariant, not a tuning knob: **Excel wins.**
Excel data is extracted real data from a book-of-record system (SimCorp
Dimension). A best-effort market feed must not silently overwrite the
authoritative record. Concretely: *a live fetch overwrites no row whose
origin is Excel; it upserts only its own prior live rows, and inserts
where Excel is silent.*

### What the target schema already offers

- `investment_navs` has **two orthogonal provenance columns already**:
  `source` (free-text) and `basis` (`'reported'` | `'computed'`,
  nullable, ADR-0079). `basis` is **semantic** (reported vs. computed),
  not an ingest-origin marker; overloading it would corrupt a meaning
  fixed in accepted ADRs.
- `investment_navs` has the unique key
  `(investment_id, as_of_date, nav_kind)` — the exact collision grain
  for NAV/price.
- `investment_cashflows` has **no** unique constraint by design
  (ADR-0043 §1): multiple same-day flows are legitimate. Idempotent live
  cashflow ingest therefore cannot rely on a DB unique key and needs an
  explicit dedup strategy (below).
- Composition-weight tables are historised on
  `(investment_id, as_of_date, <dim>_id)` (ADR-0080).

## Decision

Add a **typed ingest-origin field** to the ingested tables and enforce
Excel precedence as a **conditional upsert**, not as configurable policy.

### Ingest-origin is a new, typed field

Add `ingest_origin` (TEXT NOT NULL, CHECK
`ingest_origin IN ('excel', 'live', 'manual')`) to `investment_navs`,
`investment_cashflows`, and the composition-weight tables. It is
**distinct from** `source` and `basis`:

- `basis` — reported vs. computed (analytics semantics). Untouched.
- `source` — free-text provenance detail (e.g. `'yahoo'`,
  `'simcorp-export-2026-06'`). Retained.
- `ingest_origin` — **the producer that wrote the row**. This is the
  field precedence is decided on.

Migration backfills existing rows to `ingest_origin = 'excel'` (their
true origin) — a definite backfill, unlike the nullable `basis`.

### Excel precedence as a conditional upsert (a system invariant)

For **NAV / price** (unique-keyed):

- On live write for `(investment_id, as_of_date, nav_kind)`:
  - If a row exists with `ingest_origin = 'excel'` → **skip** (Excel is
    authoritative; not an error, a recorded no-op).
  - If a row exists with `ingest_origin = 'live'` → **update in place**
    (the live producer refreshes its own prior value).
  - If no row exists → **insert** with `ingest_origin = 'live'`.

This is a single conditional upsert (`INSERT ... ON CONFLICT ... DO
UPDATE ... WHERE existing.ingest_origin = 'live'`, with the `'excel'`
case falling through to no-op). It is a **fixed invariant** — deliberately
**not** a config table — because the precedence is a data-integrity rule
of the system, not a per-tenant calibration. This is the intentional
counter-example to the project's usual "prefer config tables" lean:
config is for things that legitimately vary; the authority of the
book-of-record does not.

For **cashflows** (no unique key): idempotency uses a deterministic
dedup key computed rule-based from
`(investment_id, flow_timestamp, flow_type, flow_kind, amount, source)`
— never LLM-formed, consistent with the key-forming discipline. A live
cashflow whose dedup key matches an existing `ingest_origin = 'excel'`
row is skipped; matching a prior `'live'` row is a no-op (already
present); otherwise inserted as `'live'`. Excel cashflows are never
touched.

For **composition weights**: same rule on the historised natural key —
a live snapshot for a date already carrying an Excel snapshot is skipped;
its own prior live snapshot is replaced; an untouched date is inserted.

### The normalised write path

The live-import writer consumes `NormalizedSeries` / `NormalizedQuote`
from the provider port (ADR-0091) and calls the existing
`InvestmentService` write workflows, extended to (a) accept
`ingest_origin` and (b) apply the conditional-upsert guard. The Excel
path continues to write `ingest_origin = 'excel'` and is otherwise
unchanged — the Excel extractor remains the reference contract.

### Scope of replication

Per decision, the live path replicates the Excel data basis **as fully as
the provider allows**: NAVs/prices **and** all cashflow types
(`dividend`, `coupon`, etc. for listed instruments). Purchases/sales of
positions (changing holdings) remain **out of scope** and deferred.

## Consequences

- Excel authority is guaranteed at the row level by a single
  well-tested guard; no live job can corrupt book-of-record data.
- `ingest_origin` gives audit and UI a clean way to show "where did this
  number come from", satisfying the compliance-auditability posture
  (MaRisk / BAIT).
- A migration (`b021`) adds `ingest_origin` across the three table
  families with an `'excel'` backfill and CHECK constraints.
- Cashflow idempotency requires the deterministic dedup key to be
  implemented and unit-tested as a pure function (a natural companion to
  `test_irene_key_forming_pure.py`).
- Regression tests assert: a live write never mutates an `'excel'` row
  (NAV, cashflow, weights); a re-run of the same live fetch is a no-op
  (idempotency).

## Alternatives considered

- **Overload `basis` or `source` to carry origin.** Rejected: `basis`
  has a fixed analytics meaning in accepted ADRs; `source` is free-text
  and cannot back a reliable precedence predicate or CHECK constraint.
- **Configurable precedence per tenant/source.** Rejected: the authority
  of the book-of-record is a system invariant, not a calibration; a knob
  here invites a foot-gun (a tenant configuring live-over-Excel and
  corrupting authoritative data).
- **Last-write-wins.** Rejected outright: would let a market feed
  overwrite SimCorp-sourced truth.
- **A DB unique constraint on cashflows to get free idempotency.**
  Rejected: contradicts ADR-0043 §1 (legitimate same-day multiple
  flows); handled with an explicit deterministic dedup key instead.
