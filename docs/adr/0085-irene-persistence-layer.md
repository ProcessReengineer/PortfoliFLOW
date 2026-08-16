# ADR-0085: Irene Persistence Layer — Typed Watch State, Append-Only Findings, and Tenant-Scoped Schedule

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** Feature #033 (Decision Console / Irene)
- **Tags:** decision-console, irene, persistence, data-model, multi-tenancy, rls, audit

---

## Context

Feature #033 introduces the **Decision Console**, a new top-level surface
whose proactive agent, **Irene**, converts internal portfolio state and
external RSS signals into structured *findings* on a slow, deliberate
cadence. Unlike **Shirley** (reactive, streaming, conversational), Irene
runs as a scheduled, non-streaming producer and must persist three
distinct kinds of state. Shirley's feature set is explicitly unchanged by
this ADR; she receives no heartbeat and no new persistence.

The heartbeat (ADR-0086) computes deltas by comparing the current world
against a stored "prior". The design intent (handover §2, §3.3) is that
this prior is *functional memory* — the thing edge-triggering diffs
against — not merely an audit trail. Three storage concerns emerge, and
they have different write patterns and retention semantics:

1. **Watched world state.** A small typed object per monitored subject
   (e.g. an AnlV class coverage, an SAA bucket). It is machine-written and
   **overwritten on every beat**. It stores *magnitude*, not merely a
   breach boolean, so that a material escalation *within* an existing
   state (50.5% → 58%) is distinguishable from noise (50.5% → 50.6%).

2. **Findings / cards.** The surfaced decision-support artifacts. Each is
   an **event with a lifecycle** — born on a rising edge, assigned a user
   resolution (acted / dismissed / acknowledged), completed as an audit
   record. This is append-only.

3. **Cadence configuration.** Per-tenant schedule for when Irene runs.
   This is the calibration/settings interface (ADR-0086), not a
   deployment artifact, so it lives in the database.

Pressing these into one table would overload a single field to mean both
"current measurement" and "historical event", violating the project's
one-logical-concern discipline. All three are tenant-scoped and must obey
RLS via the application-role switch established in ADR-0078.

The existing `DataStoreEntry` model is the correct *pattern* reference
(tenant_id FK, JSONB payload, `created_at`/`updated_at`, unique
constraints, RLS policy) but is a DataFrame store and is **not** reused
directly.

## Decision

Introduce three new tables in a single Alembic migration, each with a
`tenant_isolation` RLS policy consistent with ADR-0035/0078.

### `irene_watch_state` — typed world state (upsert)

- Primary identity: unique `(tenant_id, subject_key)`.
- `subject_key TEXT NOT NULL` — a **stable, deterministic, typed string
  identifier** for the monitored subject (e.g. `anlv:16`, `saa:equity`,
  `rss:cluster:<hash>`). Key formation is rule-based and **never
  LLM-generated** (see ADR-0086 for the enforced invariant).
- `magnitude NUMERIC` — the measured quantity (e.g. coverage percent),
  nullable for non-scalar subjects.
- `band TEXT` — derived state band (`informational` / `noteworthy` /
  `critical`); deterministically assigned, never LLM-set.
- `acknowledged_at TIMESTAMPTZ NULL`, `acknowledged_magnitude NUMERIC
  NULL` — the state the user has already seen. Edge and re-trigger deltas
  are computed **against these fields**, not against the previous raw
  heartbeat.
- `last_seen_at TIMESTAMPTZ NOT NULL` — last beat that observed the
  subject.
- Standard `tenant_id` FK, `created_at`, `updated_at`.

Written by upsert once per beat per observed subject.

### `irene_finding` — findings / journal (append-only)

- `id` surrogate PK; `tenant_id` FK.
- `subject_key TEXT NOT NULL` — reference to the subject (not an FK to
  `irene_watch_state`; findings outlive state rows and RSS-only findings
  may reference transient buckets).
- `payload JSONB NOT NULL` — the `surface_finding` contract (ADR-0088:
  trigger, finding, basis, urgency_suggestion, options, evidence_refs).
- `urgency SMALLINT NOT NULL` — the **final** urgency after the
  deterministic floor (ADR-0088), not Irene's suggestion.
- `band TEXT NOT NULL` — derived from final urgency.
- `resolution TEXT NOT NULL DEFAULT 'open'` — one of
  `open` / `acted` / `dismissed` / `acknowledged`.
- `resolved_at TIMESTAMPTZ NULL`, `resolved_by UUID NULL`.
- `created_at TIMESTAMPTZ NOT NULL`.

Append-only: findings are never mutated except to record resolution.

### `irene_schedule` — cadence configuration (settings interface)

- Unique `(tenant_id, user_id)` where `user_id UUID NULL`.
- `user_id` is **nullable and present from day one** but **not populated
  in v0**: v0 configures cadence at tenant level only. The nullable column
  draws the per-user seam without a later schema change.
- `cadence TEXT NOT NULL` (v0: `daily`), `preferred_hour SMALLINT`,
  `timezone TEXT NOT NULL`.
- `enabled BOOLEAN NOT NULL DEFAULT true`.
- `next_due_at TIMESTAMPTZ NOT NULL`, `last_beat_at TIMESTAMPTZ NULL`.
- `event_profile JSONB NOT NULL DEFAULT '{}'` — reserved for per-tenant
  event-trigger selection (v1); empty and unused in v0, so the event
  seam requires no later migration.
- Standard `tenant_id` FK, `created_at`, `updated_at`.

### The Journal is the union of `irene_watch_state` and `irene_finding`

`watch_state` provides the functional "prior" for delta computation;
`irene_finding` provides the audit history. There is no separate
"journal" table.

## Consequences

- Delta computation (ADR-0086) reads `acknowledged_magnitude` from
  `watch_state`; the edge/re-trigger contract has a concrete home.
- The append-only finding table is a clean, immutable audit trail: every
  surfaced card and its resolution is reconstructable.
- Three RLS policies are added in one migration; the CLI provisioning
  path (ADR-0078) must open `tenant_context(enforce_rls=True)` when
  seeding or inspecting these tables.
- Falling-edge de-escalation (ADR-0086) resets `acknowledged_*` on
  `watch_state` **and** appends an "all-clear" finding deterministically
  capped at `informational`.
- No reuse of `DataStoreEntry`; a small dedicated repository module per
  table follows the established repository pattern.

## Alternatives Considered

- **Single table with a status field.** Rejected: overloads one field to
  mean both current measurement and historical event; conflicts with
  append-only audit needs and one-concern discipline.
- **Cadence in systemd/cron unit files.** Rejected: cadence is a
  per-tenant (later per-user) domain concern that owners must edit in
  settings, not a fixed infrastructure tick. See ADR-0086.
- **Reusing `DataStoreEntry` JSONB rows.** Rejected: it is a DataFrame
  store with different semantics; typed columns (magnitude, band,
  acknowledged_*) are needed for deterministic diffing.

## Compliance & Audit Relevance

- **Auditability (MaRisk, BAIT/VAIT):** the append-only `irene_finding`
  table records every surfaced decision-support card, its computed
  urgency, and the PM's resolution — a complete, immutable trail of what
  the system said and what the human decided. Decision authority remains
  with the PM; Irene provides decision support, not regulated
  advice.
- **Tenant isolation (DORA, data governance):** all three tables carry
  RLS `tenant_isolation` policies per ADR-0035/0078; cross-tenant leakage
  is prevented at the database layer.
- **Reproducibility:** magnitude and `acknowledged_magnitude` are stored
  explicitly so that any historical finding's trigger condition can be
  re-derived and explained to an examiner.

## Revision History

- 2026-07-02 — Proposed.
- 2026-07-11 — Accepted against the shipped code. Implemented 2026-07-02:
  migration `b019` (`2026_07_02_1200_b019_add_irene_persistence.py`), the
  `core/models/irene_*` ORM models (`irene_watch_state`, `irene_finding`,
  `irene_schedule`), and their tenant-scoped repositories.
