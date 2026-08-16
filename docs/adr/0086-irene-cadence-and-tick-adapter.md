# ADR-0086: Irene Cadence and Tick Adapter — Database-Driven Due Evaluation with a Swappable Tick Source

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Feature #033 (Decision Console / Irene)
- **Tags:** decision-console, irene, scheduling, multi-tenancy, concurrency, synthesis

---

## Context

Irene must run on a cadence that **each tenant** (and, later, each user)
controls from settings — a domain rhythm, not a fixed infrastructure
frequency. A single systemd frequency for the whole process cannot
express per-tenant cadence, and an in-process loop bound to uvicorn
inherits the multi-worker fan-out problem already flagged in the codebase
("multi-worker deployments will need Redis"): the loop would fire once
per worker.

At the same time, the *act* of ticking (a periodic "check who is due") is
dumb, tenant-blind infrastructure. Conflating "what ticks" with "who
decides a beat is due now" is the source of the difficulty.

Irene's synthesis is also structurally unlike Shirley's turn.
`ai_service_core.stream_response` is a streaming chat loop guarded by a
process-wide `_TURN_LOCK`, with no `tool_choice` or `response_format`
support. A heartbeat needs a **non-streaming, structured** call that
returns `surface_finding` invocations, and it must not contend with
Shirley's live chat for a shared lock.

## Decision

Separate the two jobs: a dumb fixed **tick source** and a
database-driven **due evaluation**. Persist cadence in `irene_schedule`
(ADR-0085).

### Due evaluation lives in the database + domain layer

- A tick triggers: "for each tenant whose `next_due_at <= now()` and
  `enabled`, run a beat and recompute `next_due_at` from its cadence."
- The evaluation query and the beat handler are the tested domain; they
  are **agnostic to what triggered the tick**.

### Tick source is a thin, swappable adapter

- v0: an **external systemd timer** invokes a tenant-blind CLI command,
  `cli/irene_tick.py`, which runs the due-evaluation query and beats each
  due tenant. This follows the existing CLI pattern (`cli/bootstrap.py`,
  `cli/inspect_tenant.py`): a standalone process that opens
  `tenant_context(enforce_rls=True)` per tenant on the appropriate
  engine (ADR-0078).
- Because due logic is fully in the query + lock, the tick source is
  replaceable (e.g. in-process loop) **without touching domain logic**.
  This is an explicitly documented seam, not an accident.

### Per-tenant overlap protection via advisory lock

- Each tenant beat is claimed with `pg_advisory_lock` (a stable hash of
  the tenant UUID). Two overlapping tick firings, or a future
  multi-worker tick, cannot double-run a tenant's beat. This is the same
  primitive that later serves multi-worker leader election, adopted now
  at zero extra cost.

### Non-streaming synthesis path

- A new `run_synthesis(...)` entry point (Irene-scoped, reusing the
  OpenRouter client) issues a **single non-streaming** request with
  `tools=[surface_finding]` and `tool_choice="auto"`.
- `auto` (not forced) is required: **zero calls = silence**, the
  "nothing material" case falls out natively with no special-casing.
- Because the beat runs in a **separate process** from uvicorn, the
  process-wide `_TURN_LOCK` is irrelevant by construction; no lock is
  shared with Shirley. Shirley's streaming path is untouched.

### Event triggers deferred to v1

- v0 uses a pure time trigger. The `event_profile` column (ADR-0085) and
  the tick/due seam are drawn so that event-driven beats are additive,
  requiring no schema or scheduler redesign.

## Consequences

- Two tenants with different rhythms are two rows, not two timers;
  cadence is data.
- The v0 deployment adds one systemd timer unit invoking one CLI command;
  no long-lived scheduler process, no new runtime dependency.
- The synthesis path must be built new (there is no existing structured/
  non-streaming path); it does not modify `stream_response`, so Shirley's
  behaviour and lock semantics are unchanged.
- `run_synthesis` is the integration point for ADR-0088 (surface_finding
  contract, urgency floor); this ADR fixes only the execution and
  concurrency shape.

## Alternatives Considered

- **In-process asyncio loop in `web/main`.** Rejected for v0 default:
  couples to uvicorn lifecycle, re-shares `_TURN_LOCK` in-process, and
  fires per worker under multi-worker, forcing premature leader election.
  Retained as a *possible* future tick source behind the same seam.
- **APScheduler.** Rejected: a framework dependency for a single daily
  job; in-process variant inherits the loop's problems, out-of-process
  variant is the CLI approach without the extra dependency.
- **Forced `tool_choice`.** Rejected: would compel a finding even on a
  calm day, destroying "zero calls = silence".
- **Sharing Shirley's `_TURN_LOCK`.** Rejected: a batch beat would block
  every tenant's live chat for the synthesis duration.

## Compliance & Audit Relevance

- **Operational transparency (BAIT/VAIT):** an external CLI beat produces
  a log line and exit code per run — "did the beat run, for which
  tenants, with what outcome" is inspectable, unlike hidden in-process
  task state.
- **Determinism & reproducibility:** cadence and due state are persisted
  (`next_due_at`, `last_beat_at`), so the schedule on any given day is
  reconstructable. `tool_choice="auto"` keeps silence as the audited
  default state rather than a suppressed finding.
- **Isolation (DORA):** advisory-lock claiming plus per-tenant
  `tenant_context` prevents cross-tenant beat interference and double
  execution.

## Revision History

- 2026-07-02 — Proposed.
- 2026-07-11 — Accepted against the shipped code. Implemented 2026-07-02:
  `services/irene/scheduling.py` (database-driven due evaluation),
  `cli/irene_tick.py` (out-of-process tick adapter), and the systemd units
  under `docs/deploy/` (`irene-tick.service`).
