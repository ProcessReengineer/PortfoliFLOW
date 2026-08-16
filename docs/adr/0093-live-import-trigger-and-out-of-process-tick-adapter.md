# ADR-0093: Live-Import Trigger and Out-of-Process Tick Adapter — A Swappable Trigger, Per-Tenant Advisory Locking, and a System Actor

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Live Data Import (provider-agnostic ingest)
- **Tags:** market-data, data-import, scheduling, multi-tenancy, concurrency, audit

---

## Context

A live import must be **triggered** — on a cadence and/or on demand — to
call the provider port (ADR-0091), normalise, and write via the ingest
contract (ADR-0092). Two properties of the providers force the trigger's
shape:

- **Bloomberg `blpapi` is synchronous, needs IP-whitelisting, and holds a
  local session.** It does not belong inside the async FastAPI request
  workers, where blocking work freezes tenants. The refresh is therefore
  naturally an **out-of-process job**, not a request handler.
- **Provider calls are slow and rate-limited**; a refresh over many
  investments is a batch, not a synchronous user interaction.

The platform already solved an almost identical problem for Irene: a
periodic, tenant-aware, overlap-protected job triggered by a dumb
external tick.

### What exists and is reusable

- **Irene's cadence/tick architecture (ADR-0086):** a *dumb fixed tick
  source* (systemd timer → `cli/irene_tick.py`) separated from
  *database-driven due evaluation*; per-tenant overlap protection via
  `pg_advisory_lock`; the tick source is an explicitly documented,
  swappable seam. `docs/deploy/irene-tick.{service,timer}` show the
  deployment shape.
- **System/sentinel actor precedent:** bootstrap and Irene write rows
  without a human user; the `created_by` NOT NULL FK is satisfied by a
  system principal.
- **Tenant context + RLS discipline (ADR-0078):** per-tenant jobs open
  `tenant_context(enforce_rls=True)` per tenant.
- **The market-linked predicate (ADR-0090):** the set of investments a
  refresh should even attempt.

## Decision

Reuse the Irene trigger topology 1:1 for market data: an **out-of-process
job**, `cli/market_data_tick.py`, invoked by a **swappable external
trigger**, with **per-tenant advisory locking** and a **system actor**
for writes. Cadence is database-driven and per-tenant.

### Separate the dumb trigger from due evaluation

- The **trigger** (v0: a systemd timer, mirroring `irene-tick.timer`) is
  tenant-blind infrastructure that just says "run now".
- **Due evaluation** lives in the DB + domain layer: "for each tenant
  whose market-data refresh is enabled and due, refresh its
  live-eligible investments and recompute next-due." Because due logic is
  in the query, the trigger is replaceable (manual "Refresh now" button,
  in-process loop) **without touching domain logic** — a documented seam,
  not an accident.

### Per-tenant cadence in a config table

A tenant-scoped `market_data_schedule` table (nullable `user_id` for
future per-user config), directly analogous to `irene_schedule`
(ADR-0085): `enabled`, cadence, `next_due_at`, last-run metadata. Cadence
**is** legitimate per-tenant calibration, so — unlike the Excel-precedence
invariant of ADR-0092 — it correctly lives in a config table, consistent
with the project's config-over-constants lean.

### Per-tenant overlap protection

Each tenant's refresh is claimed with `pg_advisory_lock` on a stable hash
of the tenant id, so overlapping ticks (or a future multi-worker
deployment) cannot double-run a tenant. Identical mechanism to ADR-0086.

### A dedicated system actor satisfies audit FKs

Live writes have no human user. A dedicated **market-data system
principal** (per-tenant or a global service principal, following the
bootstrap/Irene precedent) provides `created_by`. Every written row
additionally carries `ingest_origin = 'live'` (ADR-0092) and a `source`
naming the provider and fetch — a complete audit trail
(MaRisk / BAIT posture).

### On-demand trigger shares the same core

A manual "Refresh now" (web action) invokes the **same** due-evaluation /
refresh core for one tenant, bypassing only the cadence gate. It must not
run blocking provider work in the request; it enqueues / invokes the
out-of-process job. This keeps the async web layer clean and consistent
with the async-first port (ADR-0091).

### Bloomberg placement

Because the job is out-of-process, the synchronous `blpapi` adapter runs
where it belongs — never in the async web workers. The async port still
holds (ADR-0091): the CLI job is itself async (it writes via async
repositories and can fan out over providers concurrently), and the
Bloomberg adapter bridges internally via `asyncio.to_thread`.

## Consequences

- The refresh job is testable at the domain level (due evaluation, lock,
  system-actor write) with fake adapters, no live entitlement in CI —
  mirroring `tests/cli/test_irene_tick.py`.
- Deployment reuses the systemd timer/service pattern already documented
  for Irene; operators learn one mechanism.
- Multi-worker safety is inherent (advisory lock), avoiding the
  "fires once per worker" trap called out in ADR-0086.
- A migration (`b022`) adds `market_data_schedule` with RLS policy and
  the per-tenant seed parity expected by
  `test_seed_tenant_defaults` / ADR-0077.
- Future **event-driven** triggers (e.g. intraday) are a new trigger
  adapter against the same due/refresh core — the seam is drawn now, the
  implementation deferred, exactly as Irene deferred event triggers.

## Alternatives considered

- **In-process scheduler inside uvicorn.** Rejected: multi-worker
  fan-out double-runs; blocking Bloomberg would freeze the loop. Same
  reasoning as ADR-0086.
- **Synchronous refresh in the request handler.** Rejected: slow,
  rate-limited, blocking provider work does not belong in an async
  request; violates the async-web-layer discipline.
- **A bespoke scheduler distinct from Irene's.** Rejected: needless
  second mechanism; the Irene tick topology already fits exactly and is
  proven.
