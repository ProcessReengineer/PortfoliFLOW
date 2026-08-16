# ADR-0055: Cash as Residual in AUM Coverage Engine

- **Status:** Accepted
- **Date:** 2026-05-19
- **Deciders:** PortfoliFLOW project owner
- **Tags:** schema, limits, aum, cash, engine-contract, anlagegrenzen, phase-7

---

## Context

The investment-limit monitoring feature (Anlagegrenzen-Überwachung)
expresses every limit as a maximum share of **AUM** (Assets Under
Management). For institutional investors such as German
Versorgungswerke, AUM is the sum of all invested capital plus the
operational cash position held at custodians. A typical Satzungsgrenze
reads `"max. 30 % Aktien"`, where the 30 % is interpreted against
total AUM, not against the invested-only base.

This raises the question of how to represent cash inside the
PortfoliFLOW data model. Three options were considered:

**Option A — Cash as a first-class investment row.** Every tenant
gets one (or more) ``investments`` rows with ``investment_type =
'cash'``, NAV time-series carried in ``investment_navs``. The
limit engine treats the cash row like any other investment.

**Option B — Cash as a dedicated table (``cash_positions``).**
Separate domain object with its own time-series. Cleanly modelled
but introduces a parallel persistence path.

**Option C — Cash as the residual of an authoritative AUM
time-series.** The tenant imports an ``aum_total`` daily series.
The engine computes ``cash(t) = aum_total(t) − Σ nav(t)`` on the
fly. No cash row, no cash table.

Two additional facts shape the decision:

1. The Excel-import schema already carries NAV time-series per
   investment and an ``interest rates`` reference series. Adding
   another reference series (``AUM total``, identical shape to
   ``interest rates``) is a one-sheet, one-column extension —
   no new column namespace, no new parser branch.
2. The investments table (b006) has a hard ``CHECK`` constraint on
   ``investment_type`` enumerating exactly seven types
   (``private_equity, private_debt, real_estate, infra_equity,
   listed_equity, listed_bonds, other``). Adding ``cash`` would
   require schema migration and re-evaluation of every code path
   that branches on ``investment_type`` — including the SAA
   optimiser, the IRR/multiples providers, the cashflow engine,
   and the audit-and-isolation tests.

The institutional reality reinforces option C. Versorgungswerk
treasurers do not maintain a per-day NAV for cash in the same way
they do for funds — they know their **total AUM** at month-end from
the custodian reconciliation, and that figure is what the
supervisory authority (e.g. BaFin) and the actuarial reports refer
to. The operational cash balance is what's left after subtracting
the invested book.

---

## Decision

**Cash is not persisted as a domain entity.** PortfoliFLOW models
AUM as an authoritative tenant-scoped daily time-series; cash is
computed at engine evaluation time as the residual.

### Schema

A new tenant-scoped table ``portfolio_aum``:

| Column | Type | Notes |
|---|---|---|
| ``id`` | UUID | PK, default ``gen_random_uuid()`` |
| ``tenant_id`` | UUID NOT NULL | FK to ``tenants.id``, RLS-policed |
| ``as_of_date`` | DATE NOT NULL | Calendar day the figure applies to |
| ``aum_eur`` | NUMERIC(20, 4) NOT NULL | Total AUM in EUR (committed-base currency) |
| ``source`` | TEXT NOT NULL | Provenance discriminator: ``'excel_import'``, ``'manual'``, ``'gp_report'`` (future) |
| ``created_by`` | UUID NOT NULL | FK to ``users.id`` |
| ``created_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |
| ``updated_at`` | TIMESTAMPTZ NOT NULL | ``server_default NOW()`` |

Constraints:

- ``UNIQUE (tenant_id, as_of_date)`` — one figure per day per tenant.
- ``CHECK (aum_eur > 0)`` — AUM must be strictly positive. A zero
  or negative AUM is a data error, not a valid portfolio state.
- ``apply_tenant_rls('portfolio_aum')`` — same RLS pattern as
  ``sectors``, ``regions``, ``investment_navs``.
- Audit trigger from b001.
- Index on ``(tenant_id, as_of_date DESC)`` to make the engine's
  "latest figure" lookup an index-only scan.

The Excel import populates one row per day of the
``AUM`` sheet (daily granularity matches ``interest rates`` and
NAV sheets). For Phase-6 multi-tenant ingestion, the row count
will be ~5500 rows per tenant for a 15-year horizon — well under
any volume concern.

### Engine contract

At each evaluation date ``t`` the limit-coverage engine performs:

```
aum_t      := portfolio_aum.aum_eur where as_of_date = t
nav_sum_t  := Σ investment_navs.nav_eur where as_of_date = t and is_active
cash_t     := aum_t − nav_sum_t          # may be small or even briefly negative
coverage_class_c := Σ {nav_eur(i) for i in investments where class(i) = c} / aum_t
```

Cash is **never** added to a numerator. The "AnlV unallocated" or
"Satzung unallocated" bucket — the V1 reine-Engine-Fallback for
investments whose class is null — is a separate concept handled
by the engine, not by cash arithmetic. ADR follow-up #2
(Limit-Set Historisierung) and the engine design in Konzeptchat #2
nail the unallocated semantics.

### Currency

All NAV figures already land in ``investment_navs`` as EUR-converted
values (this is implicit in the b006 schema; investments carry a
``currency`` attribute but NAVs are reported in EUR for actual
sheets — see ADR-0009 §3). AUM rows are EUR. No multi-currency
arithmetic in V1.

### Missing days

If ``aum_eur`` for ``as_of_date = t`` is absent (gap in the import
or evaluation date outside the imported range):

- **In coverage evaluation:** carry forward the latest known
  ``aum_eur`` where ``as_of_date <= t``. If no such row exists, the
  engine raises ``CoverageInputMissing`` and the operator sees a
  surfaced error in the UI. No silent zero-fill.
- **In import validation:** absence of an ``AUM`` sheet is a hard
  import error in V1. The sheet is required.

---

## Rationale

**Why not option A (cash as investment row)?**

Three concrete cost centres:

1. The ``investment_type`` ``CHECK`` constraint becomes a moving
   target. ADR-0043 §2 pinned the seven-type enumeration as a
   deliberate constraint on the Phase-4 schema and downstream
   engines. Loosening it for cash invites every future asset class
   to lobby for inclusion.
2. The IRR provider, the cashflow provider, and the multiples
   provider all skip rows that are not contributing-cash investments
   in the J-curve sense. Cash would need explicit exclusion in each
   of them. Six providers × one exclusion branch each is six places
   for the next refactor to forget a branch.
3. The SAA optimiser already handles cash as the risk-free asset
   via the ``risk_free_rate`` parameter sourced from the
   ``interest rates`` sheet. Putting cash into the investments table
   would either duplicate that role or force a new "is this row
   the cash row for SAA purposes" predicate.

**Why not option B (cash as dedicated table)?**

A ``cash_positions`` table would be a near-clone of the proposed
``portfolio_aum`` table but with the opposite semantic load: the
operator would have to maintain cash explicitly, and a discrepancy
between ``cash + Σ NAV`` and the actual custodian-reported AUM
would be the operator's problem to reconcile. This inverts the
real-world workflow: the figure the treasurer trusts is the
custodian's AUM statement; the cash residual is a computed artefact.

Option C makes the data model honour that workflow: import the
trusted figure, compute the residual.

**Why an AUM table at all — why not infer AUM from NAVs?**

Two reasons:

1. The operator has cash they want represented in the AUM base of
   the limit. Inference from NAVs alone gives a coverage base that
   excludes cash and therefore overstates every coverage ratio by
   ``aum / nav_sum`` — typically 5–25 % of relative error.
2. The custodian-reported AUM is the supervisory figure. PortfoliFLOW
   reports limit coverage to the same audience that reads the
   custodian statements; using a different denominator would make
   reconciliation impossible.

**Why daily granularity rather than month-end?**

The Excel time-series for NAVs and interest rates are daily. Aligning
AUM to the same granularity keeps the engine's date-keying trivial
(single ``WHERE as_of_date = :t``) and avoids interpolation logic.
For tenants who only know AUM at month-end, the importer's
forward-fill semantics (described above as carry-forward) handles
the gap — the operator imports 12 month-end figures and the engine
treats every day in the month as carrying that figure forward.

---

## Consequences

### Positive

- The investments table stays clean. The ``investment_type`` enum
  remains stable, all existing engines continue to work without
  cash-aware branches.
- The mental model matches the Versorgungswerk workflow: trust the
  custodian's AUM figure, let the system compute what the rest of
  the position implies for cash.
- The same ``portfolio_aum`` table will absorb future ingestion
  paths — GP-report scrapers, custodian SFTP, manual UI entry —
  without schema change. The ``source`` discriminator is the seam.
- Cash positivity is **not** enforced by the schema. A briefly
  negative residual (e.g. on a single day when a capital call
  exceeded the cash float and was funded by overdraft) is allowed
  to surface. Negative residuals exceeding an operator-defined
  threshold can be surfaced as warnings in V2 without a schema
  change.

### Negative

- The operator cannot drill into "where did the cash go" inside
  PortfoliFLOW. The residual is opaque. This is the right tradeoff
  for V1, but a future feature may want a cashflow-bridge view
  that decomposes the AUM change into NAV moves, distributions,
  and contributions. Schema impact at that point: zero — that view
  is a query over the existing ``investment_cashflows`` and
  ``portfolio_aum`` tables.
- AUM data quality becomes a critical dependency. A missing import
  day silently degrades to the carry-forward value. The import
  pipeline must surface the date-range coverage of the AUM sheet
  prominently in the operator-visible import summary.

### Neutral

- The ``AUM`` sheet in the Excel V2 format becomes a required
  recognised sheet. The Excel ADR (ADR-0009) is amended in the same
  PR that lands this migration to list it among the
  ``MARKET_REFERENCE_SHEETS`` family — same shape as
  ``interest rates``, independent column namespace, daily rows.

---

## Implementation pointers

- New migration: ``db/migrations/versions/YYYY_MM_DD_HHMM_bNNN_add_portfolio_aum.py``
  (the bNNN serial follows the existing chain; b007 is the last
  domain migration prior to this work).
- New ORM model: ``core/models/portfolio_aum.py``.
- New repository: ``core/repositories/portfolio_aum_repository.py``,
  with methods ``upsert_many``, ``get_for_date``, ``get_range``,
  and ``latest_as_of``.
- Importer: extend ``modules/front_office/data_import.py`` to
  recognise ``"AUM"`` as a member of ``MARKET_REFERENCE_SHEETS``
  with the canonical DataStore key ``"aum"``.
- Tests: roundtrip Excel → repository → engine assert, plus the
  carry-forward unit test, plus a regression test that the
  investments table's ``investment_type`` enum has not been
  extended (guard against future drift back toward option A).

---

## Related ADRs

- ADR-0009 — Excel V2 import format (extended in the same PR to add
  the ``AUM`` sheet recognition)
- ADR-0035 — Multi-tenant operation with RLS (template for the new
  table's RLS policy)
- ADR-0043 — Flat-polymorphic investments table (the
  ``investment_type`` enum constraint that option A would have
  perturbed)
- ADR-0056 — Limit-Set Historisierung (the next ADR in this stream;
  the engine that consumes ``portfolio_aum``)
- ADR-0057 — AnlV classification as 1:1 attribute (the third ADR;
  the classification dimension that AnlV limits aggregate over)
