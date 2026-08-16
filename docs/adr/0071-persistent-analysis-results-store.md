# ADR-0071: Persistent Analysis-Results Store — Run-Bound Results for Shirley

- **Status:** Proposed (stub — to be expanded before implementation)
- **Date:** 2026-06-01
- **Deciders:** PortfoliFLOW project owner
- **Tags:** persistence, schema, scraper, shirley, tools, trust, multi-tenant, audit

> **Stub.** Records the decision direction and the questions to answer before
> implementation. Lighter than a full ADR by intent. Tracked as roadmap item
> **B7**.

---

## Context

ADR-0069 and ADR-0070 expose **deterministic, recomputable** analyses to
Shirley — the tool recomputes from Postgres, so no user-initiated run is
required. A second class of results is **not** reproducible from a pure read:
they are produced by a user-initiated run, often parameterised, sometimes with
an external fetch, and are not derivable again on demand.

The motivating case is the **Report Scraper**. Today it persists nothing: the
route stores runs in an in-memory per-app `OrderedDict`
(`request.app.state.scraper_runs`), LRU-evicted and dropped on session end
(`web/routes/scraper.py`: "persistence is explicitly deferred"). For Shirley
to "see what the user saw," the result must first be stored. The persistence is
the real work; the read tool is trivial afterwards.

The same shape will recur for any future run-bound result a user wants to keep
(e.g. an optimizer scenario), so a per-feature persistence path would be
premature duplication.

A ready-made schema shape already exists in the core: `DataStoreEntry` stores a
named DataFrame as a JSONB payload under a tenant context with a `meta` JSONB
column. The web side does not use it (ADR-0041), but its shape — named,
tenant-scoped, typed, JSONB — is exactly what a general analysis-results store
needs.

## Decision (direction)

Introduce a small, **generic, tenant-scoped analysis-results store**: a named,
`kind`-typed result persisted as JSONB, written by the producing surface at run
time and read back by Shirley. Not per-feature. The Report Scraper is the first
writer. Expose Web-side read tools `list_analysis_results` /
`get_analysis_result`, which finally give the currently-inert web
`list_analysis_results` something real to return — and a path to eventually
retire the four in-memory DataStore tools on the web surface.

## Non-Goals

- No analysis *starting* / no run orchestration — B7 is persistence + read of
  results a user already ran.
- No change to the deterministic recompute tools (ADR-0069 / ADR-0070); those
  do not write here.

## Open Questions (resolve before implementation)

1. **Trust provenance — load-bearing.** Scraper results are external-fetched
   content (`READ_EXTERNAL_UNTRUSTED`, ADR-0022). Persisting them **must not
   launder the trust level.** On re-read, Shirley must still receive them
   inside an `<external_content source=... trust="untrusted">` delimiter. The
   store therefore needs `trust` and `source` (and `fetched_at`) columns, and
   the read tool must re-wrap accordingly — this is a schema decision, not a
   tool afterthought. Define how trust is recorded and re-emitted.
2. **Schema home.** A dedicated `analysis_results` table vs. reuse of
   `data_store_entry` / `data_uploads`. Weigh a clean purpose-built table
   against avoiding yet another JSONB store. Must carry tenant id for RLS
   (ADR-0035).
3. **`kind` taxonomy.** Enumerate result kinds (`scraper_extraction`, later
   `optimizer_scenario`, …) and the per-kind payload contracts.
4. **Retention / lifecycle.** Permanent vs. TTL; relation to the audit trail
   (BAIT/VAIT). When a user deletes a run, what happens to Shirley's view?
5. **Writer integration.** Where the scraper writes (the SSE completion path)
   without coupling `services/scraper/` to FastAPI (keep it analytics-pure per
   A2 / ADR-0053).
6. **DataStore-tool convergence.** Whether the new web read tools supersede the
   in-memory `list_analysis_results` / `get_dataset_*` family on the web, and
   the retirement path (ADR-0047 Follow-ups already flag this).

## Implementation Notes (anticipated)

- New model + migration (or reuse decision per Q2); repository; RLS policy.
- Writer hook in the scraper completion path.
- `services/tools/` read tools (`READ_INTERNAL` for the tool *call*, but the
  payload re-wrapped as untrusted external content per Q1).
- Tests: persistence + RLS isolation; trust re-wrapping on read.

## References

- ADR-0069 / ADR-0070 (the deterministic-read counterpart), ADR-0053 (Report
  Scraper web surface), ADR-0022 (trust classes & delimiters — the provenance
  rule), ADR-0035 (RLS), ADR-0041 (persistence entry-points — why the in-memory
  store is web-inert), ADR-0047 (DataStore-tool retirement follow-up),
  ADR-0004 (in-memory DataStore singleton — the `DataStoreEntry` shape).
- Roadmap: B7 (this item), A2 (Report Scraper — the first writer).

---

## Revision History

| Date       | Author                     | Change                              |
|------------|----------------------------|-------------------------------------|
| 2026-06-01 | PortfoliFLOW project owner | Initial stub, status Proposed       |
