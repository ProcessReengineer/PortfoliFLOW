# ADR-0090: Investment Security Identifiers and FIGI Normalisation — A Dedicated Identifier Table as the Deterministic Join-Key to External Data Worlds

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Live Data Import (provider-agnostic ingest)
- **Tags:** investments, schema, data-import, market-data, multi-tenancy, identifiers

---

## Context

The platform's entire investment dataset enters through a single Excel
import path (ADR-0009 / ADR-0043) and is **static thereafter**. The
next capability — a provider-agnostic **live import** — must augment the
data basis of *existing* investments from external providers (Yahoo
Finance and OpenFIGI first; Bloomberg / Preqin later). Purchases and
sales of positions are explicitly deferred.

To fetch data for an investment from any external provider, that
investment must be **unambiguously identifiable** against the provider's
universe. Today the `investments` table (`core/models/investment.py`,
ADR-0043 §1) carries operational metadata — `name`, `investment_type`,
`manager_name`, `region`, `currency`, `vintage_year`,
`commitment_amount`, `anlv_code` — but **no security identifier of any
kind**. There is no ISIN, ticker, CUSIP, or FIGI anywhere in the schema.
A live import therefore has no join-key.

### The illiquidity constraint shapes the model

PortfoliFLOW's core domain is private markets. Of the seven
`investment_type` values (`private_equity`, `private_debt`,
`real_estate`, `infra_equity`, `listed_equity`, `listed_bonds`,
`other`), only `listed_equity` and `listed_bonds` are realistically
market-linked. A private-equity fund commitment **has no ISIN** and is
retrievable from no market-data provider. Any identifier model that
assumes one identifier per investment — e.g. an `isin` column on
`investments` — would be structurally `NULL` for the majority of the
book and would silently conflate "has no identifier" with "not yet
imported".

### The key-forming constraint

A security identifier is a **join-key against external worlds**. Per the
project-wide invariant that stable identifiers must be deterministic and
rule-based — never LLM-formed or heuristic (mirrored from the Irene
subject-key discipline, ADR-0087, and enforced in spirit by
`tests/regression/test_irene_key_forming_pure.py`) — identifier
resolution must be auditable and reproducible. OpenFIGI (free;
ISIN ↔ ticker ↔ FIGI) is precisely such a deterministic normalisation
service: rule-based mapping, no inference.

### What exists and is reusable

- The `investment_type` discriminator already distinguishes listed from
  private instruments — the natural predicate for "is this row
  market-linked".
- Multi-tenancy via RLS with `tenant_id` denormalised onto child tables
  (ADR-0035 §3) so RLS evaluates row-locally without a JOIN; the
  identifier table must follow the same pattern (ADR-0078).
- Audit columns (`created_by` NOT NULL, `created_at`) are the standard
  on every domain table.

## Decision

Introduce a dedicated **`investment_identifiers`** table rather than a
column on `investments`, and adopt **FIGI as the stable internal
provider join-key** derived deterministically via OpenFIGI.

### Schema: `investment_identifiers`

One row per (investment, scheme, value):

- `id` — UUID PK.
- `tenant_id` — UUID NOT NULL, FK `tenants.id` (denormalised for RLS,
  ADR-0035 §3).
- `investment_id` — UUID NOT NULL, FK `investments.id`
  `ON DELETE CASCADE`.
- `scheme` — TEXT NOT NULL, CHECK constraint over a closed set:
  `('isin', 'ticker', 'figi', 'cusip', 'internal')`. The set is
  deliberately small and extended only by successor ADR + migration.
- `value` — TEXT NOT NULL. Normalised on write (upper-cased, trimmed);
  no scheme-specific format validation is imposed at the DB layer beyond
  non-emptiness (validation of ISIN checksums etc. is an application
  concern, kept out of the constraint surface).
- `is_primary` — BOOLEAN NOT NULL DEFAULT FALSE. At most one primary per
  investment (see constraints).
- `source` — TEXT nullable — free-text provenance of the mapping
  (`'excel'`, `'openfigi'`, `'manual'`), mirroring the
  `investment_navs.source` precedent (ADR-0079 §Context).
- `created_by` — UUID NOT NULL, FK `users.id`.
- `created_at`, `updated_at` — TIMESTAMPTZ, server-defaulted.

Constraints:

- `UNIQUE (investment_id, scheme, value)` — the same identifier is not
  recorded twice for one investment.
- `UNIQUE (tenant_id, scheme, value)` **partial, WHERE scheme <>
  'internal'** — a real-world security identifier (ISIN, ticker, FIGI,
  CUSIP) maps to at most one investment within a tenant, preventing two
  investments from claiming the same ISIN. `internal` scheme is exempt
  because it is a free operator namespace.
- A partial UNIQUE index enforcing **at most one `is_primary = TRUE` per
  `investment_id`**: `UNIQUE (investment_id) WHERE is_primary`.

### FIGI is the internal provider join-key

- Operators supply the human-readable identifiers they hold — typically
  ISIN or ticker, exactly as they appear in the source of record
  (SimCorp Dimension → Excel).
- A deterministic OpenFIGI normalisation step resolves ISIN/ticker →
  **FIGI**, which is recorded as an additional `figi`-scheme row and
  becomes the stable key the market-data adapters (ADR-0091) prefer for
  provider calls. OpenFIGI resolution is rule-based; it never *invents*
  an identifier and never uses an LLM. A failed resolution is a recorded
  gap, not a fabricated mapping.

### Market-linked predicate

An investment is treated as **live-import-eligible** iff its
`investment_type ∈ {listed_equity, listed_bonds}` **and** it has at least
one primary market-scheme identifier (`isin`/`ticker`/`figi`). This
predicate lives in the investment/service layer, not as a stored column,
so it stays derivable and cannot drift. The live-import job (ADR-0093)
uses it to skip illiquid positions cleanly rather than failing on them.

### Identifiers enter through both import paths

- **Excel:** the import format (ADR-0009 / ADR-0059) gains an optional
  identifier attribute row (e.g. `ISIN`, `Ticker`) on the `Attributes`
  sheet. The `InvestmentExtractor`
  (`services/data_normalization/investment_extractor.py`) parses it into
  the normalised identifier set; blank cells produce no rows (correct for
  illiquid instruments). Identifiers recorded from Excel carry
  `source = 'excel'`.
- **Manual / live:** the CRUD surface and the OpenFIGI step can add rows
  with `source` set accordingly.

## Consequences

- Illiquid investments carry **zero** identifier rows and are naturally
  excluded from live import — no `NULL`-column ambiguity.
- A single investment can hold ISIN + ticker + FIGI simultaneously,
  supporting deterministic cross-provider addressing.
- A first migration (`b020`) creates the table, its constraints, and RLS
  policy consistent with `test_rls_schema_invariants.py`.
- The Excel import format changes are additive and backward-compatible:
  existing workbooks without an identifier row import exactly as before.
- OpenFIGI is an external network dependency; its adapter lives under the
  market-data layer (ADR-0091), **not** in the pure extractor, preserving
  the extractor's no-external-call property (ADR-0043 §3).

## Alternatives considered

- **`isin` column on `investments`.** Rejected: structurally `NULL` for
  most of a private-markets book; cannot hold ticker + FIGI
  simultaneously; conflates "no identifier" with "not imported".
- **JSONB `identifiers` blob on `investments`.** Rejected: not queryable
  under a UNIQUE constraint, so it cannot enforce "one ISIN → one
  investment"; the existing `type_specific_data` JSONB escape hatch
  (ADR-0043 §2) is reserved for genuinely type-specific fields, not
  relational keys.
- **Ticker as the internal join-key.** Rejected: tickers are exchange-
  and vendor-ambiguous (same ticker, different exchanges); FIGI exists
  precisely to be the unambiguous, free, deterministic anchor.
