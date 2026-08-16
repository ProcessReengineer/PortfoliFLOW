# ADR-0119: Watch Desk Cadence Vocabulary v1, Anchor Semantics, and Irene Schedule Seeding

Status: Accepted (2026-08-13)
Date: 2026-08-13
Supersedes: nothing (ADR-0086 remains Accepted and unedited; this ADR extends its v0 cadence vocabulary)
Related: ADR-0086 (Irene scheduling contract), ADR-0093 (tenant seeding, STD-03), ADR-0117 (built-in tick scheduler)

## Context

ADR-0086 fixed the v0 cadence vocabulary to a single member, `daily`, with the
tick/due split as the load-bearing structure: a dumb periodic tick asks "who is
due?", and `compute_next_due_at` owns all cadence arithmetic. Since ADR-0117
the default tick source is the in-process scheduler at a 60-second interval,
which is fine enough for sub-daily cadences; the external systemd path
(15-minute timers) remains adequate as well.

Two gaps surfaced during pre-release review:

1. **Vocabulary.** Institutional monitoring wants sub-daily beats. The demand
   is for hourly-interval cadences up to every six hours.
2. **Cold-start visibility.** `seed_tenant_defaults` seeds a (disabled)
   `market_data_schedule` row (STD-03) but no `irene_schedule` row. The only
   writer of `irene_schedule` is the Watch Desk cadence-save endpoint. A fresh
   tenant therefore sees a Watch Desk with "no cadence set", no "Request
   analysis now" affordance, and no beats until an operator saves the cadence
   panel once. The area looks dead out of the box.

The `cadence` column is `TEXT NOT NULL` without a CHECK constraint; validation
lives solely in `compute_next_due_at`. No migration is required for either
change.

## Decision

### §1 Vocabulary v1

`_SUPPORTED_CADENCES` grows from `{"daily"}` to:

```
daily · hourly · every_2h · every_3h · every_6h
```

Persisted lowercase, exactly as today. Validation remains solely in
`services/irene/scheduling.py`; no DB constraint is added. The Watch Desk
router's `_CADENCE_CHOICES` mirrors the vocabulary. The market-data admin
surface keeps its own `_CADENCE_CHOICES = ("daily",)` and is explicitly out of
scope — its vocabulary evolves, if ever, by its own decision.

### §2 Anchor semantics for hourly-interval cadences

`preferred_hour` becomes the **anchor hour**. For a cadence of interval N
hours, the candidate local hours are `(anchor + k·N) mod 24` for integer k,
evaluated in the schedule's IANA timezone; `compute_next_due_at` returns the
next candidate occurrence strictly after `now`. Properties:

- `daily` is the N=24 case and behaves exactly as today (unchanged behavior,
  pinned by existing tests).
- `hourly` (N=1) makes the anchor practically inert; that is accepted.
- DST is handled by `zoneinfo` as elsewhere in the module: nonexistent local
  times shift forward with the gap; ambiguous times follow fold rules. Unit
  tests cover a spring-forward and a fall-back transition for at least one
  sub-daily cadence.

### §3 UI

The cadence panel renders display labels from a label map in the Watch Desk
router (pattern: `RESOLUTION_LABELS`): "Daily", "Every hour", "Every 2 hours",
"Every 3 hours", "Every 6 hours". The current `|capitalize` rendering is
replaced (it would produce "Every_2h"). The "Preferred hour" caption becomes
"Anchor hour" with a one-line hint explaining the anchor semantics for
sub-daily cadences.

### §4 Irene schedule seeding

`seed_tenant_defaults` gains an idempotent `irene_schedule` seed row:

```
cadence = daily · preferred_hour = 8 · timezone = Europe/Berlin · enabled = TRUE
```

Seeded **enabled**, deliberately asymmetric to STD-03's disabled market-data
row, and the asymmetry is reasoned, not accidental: a market-data schedule
would fetch immediately and silently once enabled, whereas the Irene domain is
guarded by the tick scheduler's credential gate — without a resolvable LLM
credential the domain is skipped quietly per tick. Enabling the seed therefore
costs nothing until the tenant configures credentials, and the Watch Desk is
alive out of the box (schedule row exists, "Request analysis now" renders,
first credentialed tick beats).

Existing tenants receive the row through the same idempotent seed path
(insert-if-absent); no migration and no backfill script. A tenant that has
already saved a cadence is untouched.

## Consequences

- `compute_next_due_at` and `_SUPPORTED_CADENCES` extended; the pinned error
  message (`sorted(_SUPPORTED_CADENCES)`) changes and its tests update.
- New unit tests per cadence member incl. DST edges; router tests for saving a
  sub-daily cadence; seed test suite (STD cases) gains the Irene row.
- Tick granularity statement: 60 s (internal, ADR-0117 default) and 15 min
  (external systemd) both satisfy the finest cadence (`hourly`); ADR-0086's
  "tick finer than finest cadence" condition continues to hold.
- No migration; no change to the beat, floors, or synthesis.
- Roadmap: register "Watch Desk area-level deactivation" (complete on/off
  switch for the area) as a separate, ADR-bearing future item — previously
  assessed at roughly one new ADR plus three implementation prompts.

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-08-13 | PortfoliFLOW project owner | Drafted (Proposed). |
| 2026-08-13 | PortfoliFLOW project owner | Accepted; index status updated. |
