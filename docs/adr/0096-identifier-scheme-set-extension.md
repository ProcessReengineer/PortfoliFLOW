# ADR-0096: Identifier Scheme-Set Extension — Provider-Native Fund Identifiers and Human-Confirmed Mapping

- **Status:** Accepted
- **Date:** 2026-07-07
- **Deciders:** PortfoliFLOW project owner
- **Supersedes / amends:** extends the closed scheme set of ADR-0090 (which remains authoritative for everything else)
- **Implements roadmap item:** #036 — Live Data Import (deferred-items track: private-markets provider readiness)
- **Tags:** market-data, data-import, identifiers, private-markets, schema, key-forming, audit

---

## Context

ADR-0090 fixed `investment_identifiers.scheme` as a closed, CHECK-enforced
set: `{isin, ticker, figi, cusip, internal}`. That set models the listed
world plus a self-assigned scheme — correct for the first providers
(Yahoo, OpenFIGI), and the closed set is itself a deliberate
controlled-vocabulary discipline.

The next provider class breaks the assumption. **Private-markets data
providers key on proprietary identifiers**: a Preqin fund ID, a PitchBook
profile ID. A private-equity fund has no ISIN, no ticker, no FIGI — the
illiquidity principle that motivated the identifier *table* in ADR-0090
also means the *scheme set* cannot stay listed-only. As verified in the
slice-4/5 review: precisely the illiquid book the table was built for is
unreachable by the providers most relevant to it.

A second, subtler problem arrives with these schemes: **the mapping act
itself**. For listed instruments the ISIN is a universal join-key —
matching is deterministic. "Investment F *is* Preqin fund #12345" is an
entity-resolution judgement over fund name, vintage, manager, strategy —
fuzzy by nature. The key-forming discipline (deterministic/rule-based
only, never LLM-formed; ADR-0087 lineage) applies directly.

## Decision

### 1. The scheme set gains provider-native schemes

`scheme ∈ {isin, ticker, figi, cusip, internal, preqin, pitchbook}`.

- Migration (next free `b0NN`) swaps the CHECK constraint; no other
  schema change. All three ADR-0090 uniqueness rules carry over
  **unchanged and are exactly right** for provider IDs:
  - `(investment_id, scheme, value)` unique — one mapping row per fact;
  - partial `(tenant_id, scheme, value)` unique with only `internal`
    exempted — a given Preqin fund ID maps to **one** investment per
    tenant (a provider ID is an external identity, like an ISIN);
  - one primary per investment — a fund whose only identifier is its
    Preqin ID can carry it as primary.
- Values are stored verbatim modulo the repository's uniform
  normalisation (trim + upper-case). Provider IDs are digits/hyphens or
  case-insensitive tokens; the uniform rule is retained deliberately —
  one normalisation, no per-scheme special cases. Should a future
  provider issue genuinely case-sensitive identifiers, that is a
  successor-ADR concern; it is not true of Preqin/PitchBook.
- Further providers (e.g. a future `bloomberg_dl` portfolio key) extend
  the set the same way: successor ADR + CHECK swap. The set stays
  closed; growth stays deliberate.

### 2. Provider-ID mapping is a human-confirmed act

- Provider-native identifier rows enter through the **identifier CRUD
  surface** (companion feature to this ADR: view an investment's
  identifiers, add, edit, delete, set primary — the surface ADR-0090
  anticipated under "manual/live paths") with `source='manual'`, or
  later through a provider-sync step **after** human confirmation with
  `source='<provider>'`.
- Machine assistance may **propose** candidate matches (name similarity,
  vintage, manager — LLM assistance is permissible *here*, because a
  proposal is not key-forming); the **confirmation that writes the row
  is always human**. No import path may auto-write a provider-ID mapping
  from a fuzzy match. This is the key-forming discipline applied to
  entity resolution.
- The Excel path stays as ADR-0090 defined it (`ISIN` / `Ticker` rows
  only). Provider IDs do not enter through the workbook: the book of
  record documents the investment; the provider mapping is a
  platform-side act with its own audit trail.

### 3. Consequence for live-eligibility (documented outlook, not implemented now)

The market-linked predicate (ADR-0090, implemented in
`services/investments/market_linked.py`) currently means "listed type +
primary market identifier". With provider-native schemes the concept
generalises: an investment is refresh-eligible when **some capability-
matrix entry serves one of its identifier schemes** — "capability-
reachable" rather than "listed". This generalisation is deliberately
**deferred to the first private-markets adapter going live** (it has no
consumer before then and would change refresh behaviour untested).
Until then the predicate stays as implemented; this section exists so
the successor change is a planned step, not a surprise.

## Consequences

- The identifier model becomes provider-complete for the announced
  adapter roadmap (Bloomberg, Preqin, PitchBook): each future adapter
  needs only its matrix entry (ADR-0091) and — for private-markets
  providers — confirmed identifier rows, no schema work.
- The identifier CRUD surface moves from "anticipated" to **required
  companion feature**: without it, provider-native schemes are
  write-reachable only via repository calls. It is demo-relevant in its
  own right (making the mapping concept visible to an institutional
  audience).
- Two CHECK-set sources of truth exist (DB constraint, `SeriesKind`-style
  literals in code); the migration slice must update the model/validation
  literals in the same commit — the same discipline as every closed set
  in the codebase.
- The deferred predicate generalisation is now written down with its
  trigger condition; roadmap #036's deferred-items list gains this
  pointer.

## Alternatives considered

- **A generic `provider_key` scheme plus a qualifier column.** Rejected:
  splits identity across two columns, weakens the uniqueness semantics
  (the partial tenant-unique would need the qualifier folded in),
  and trades the closed set's audit clarity for a pseudo-open one.
- **Free-text scheme (drop the CHECK).** Rejected: controlled
  vocabularies are load-bearing in this codebase; silent scheme
  proliferation is exactly what the discipline exists to prevent.
- **Auto-matching provider IDs by fund-name similarity.** Rejected as a
  write path: fuzzy entity resolution is not deterministic key-forming;
  a wrong silent mapping poisons every downstream NAV/cashflow row for
  that investment. Permissible only as a human-confirmed proposal.
- **Letting provider IDs ride in the Excel workbook.** Rejected: the
  workbook documents the book of record, not platform-side provider
  plumbing; mapping needs its own audit trail (who confirmed, when) that
  the unguarded Excel upsert cannot provide.
