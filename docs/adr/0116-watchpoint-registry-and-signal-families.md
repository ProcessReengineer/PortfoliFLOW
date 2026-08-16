# ADR-0116: Watchpoint Registry and Signal Families for the Watch Desk

- **Status:** Accepted (2026-08-11)
- **Date:** 2026-08-10
- **Tags:** watch-desk, irene, watchpoints, calibration, audit, price, fx,
  liquidity, freshness, htmx, agpl-release-scope
- **Depends on:** ADR-0115 (Watch Desk rename — implemented first; this ADR
  uses the new names throughout).
- **Refines / clarifies:** ADR-0089 (the Calibration section grows from a
  read-only fact display into the calibration editor its own text kept open:
  "a Watchlist edit surface for the values remains open"), ADR-0106 (the
  monitor gains editability and new groups; its honesty rules are
  generalised, not weakened).
- **Honours:** ADR-0013/0045 (analytics purity — producers and delta/floor
  logic stay pure; the beat threads config), ADR-0056 (historisation by
  immutable `effective_from` rows), ADR-0085 (findings immutability),
  ADR-0087/0088 (delta and floor contracts — extended by new trigger types,
  never forked), ADR-0103 (materialised cash plan path — read, not
  recomputed), ADR-0107 (Cases consume findings source-agnostically —
  unchanged), ADR-0112 (scoped_settings is NOT used here; see §2).

---

## Context

What the Watch Desk observes, and at which thresholds, is today configured
nowhere in the UI:

1. **Subjects are derived, not configured.** `saa:*` / `anlv:*` subjects are
   enumerated from the effective limit sets (Excel-import only; the limit-set
   CRUD surface is the deferred B5 remainder); `rss:cluster:*` runs over the
   hardcoded `_KNOWN_TAGS` vocabulary. No UI adds, removes, or mutes a
   subject.
2. **Thresholds are code constants.** `FloorConfig`
   (`services/analytics/irene_floor.py`) carries the WARN threshold (90%),
   per-family re-trigger deltas (5.0 pp), band boundaries, floors, caps, and
   the options gate. Recalibration means editing code and redeploying,
   application-wide. The Calibration section renders three of these values
   read-only.
3. **The only editable element is the cadence** (ADR-0086).

The seams for configurability already exist: the coverage engine takes
`warn_threshold_pct` as a parameter, and `FloorConfig` is already the single
parameter object the beat threads through the pure layers. This ADR walks
through those seams. It also adds four **signal families** over data the
platform already holds (`instrument_prices`, `fx_rates`, `investment_navs`,
the materialised cash plan path), because autonomous observation of only
quota utilisation undersells the platform's central capability.

**Product decisions taken in the concept discussion (2026-08-10, binding):**

- The **asymmetry**: for `saa:*`/`anlv:*`, watchpoints are a *sensitivity
  overlay only* — subject identity and ceilings remain solely with the limit
  set. There is never a second edit point for limits. For the new signal
  families, the watchpoint *defines* the subject.
- **Selector granularity v1:** `price:*` strictly per instrument, `fx:*`
  strictly per explicit currency pair. Class-level selectors ("all held
  Listed Equities") and book-derived pair sets are successor concepts —
  commissioned to the roadmap (§Commissions), designed elsewhere.
- **No `pacing:*` family**: the TA engine (#023, Strand 3) does not exist
  yet; plan-deviation watching has no reliable reference object today.
- **Seeding**: sensible defaults on tenant creation and for the demo tenant;
  values below are v1 defaults, expected to be refined later.

## Decision

### 1. A `watchpoints` registry — historised, audited, typed

One tenant-scoped table, migration **b033**:

- **Identity + versions.** A stable `watchpoint_id` (UUID, tenant-scoped)
  with **immutable version rows** keyed `(watchpoint_id, effective_from)`,
  following the `limit_sets` pattern (ADR-0056). The current version is the
  latest `effective_from <= now()`. Edits insert a new version; nothing is
  updated in place. Retirement is a version with `retired = true` (the
  identity and history remain queryable so past findings stay explainable).
- **RLS + audit.** `apply_tenant_rls('watchpoints')`; the generic audit
  trigger IS attached (unlike `scoped_settings` — watchpoints contain no
  secrets, and threshold changes are exactly what BAIT/VAIT-grade
  explainability must capture). Versioning gives reproducibility; the audit
  trigger gives actor attribution. Both, deliberately.
- **Typed columns, no params-JSONB.** `family` (TEXT + CHECK against the
  closed set `saa | anlv | rss | price | fx | freshness | liquidity`),
  `subject_key` (for overlay rows: the derived subject it overlays; for
  defined rows: the key the producer will emit), `display_name`, `muted`
  (bool), `warn_threshold_pct` (numeric, nullable), `re_trigger_delta`
  (numeric, nullable), plus family-specific parameter columns:
  `instrument_id` (FK, price), `currency_pair` (TEXT `BASE/QUOTE`, fx),
  `drop_pct` + `window_days` (price), `move_pct` + `window_days` (fx —
  shares `window_days`), `max_age_days` (freshness), `horizon_months` +
  `min_coverage_ratio` (liquidity), `notes` (TEXT, optional).
- **The asymmetry lives in CHECKs.** Per-family CHECK constraints enforce
  which columns may be non-NULL. For `family IN ('saa','anlv','rss')` only
  the sensitivity columns (`muted`, `warn_threshold_pct`,
  `re_trigger_delta`) may be set — every defining column is forced NULL.
  A UI or repository bug cannot create a second edit point for limits; the
  schema forbids it. (`rss` overlay carries `muted` only.)

A `WatchpointRepository` returns frozen DTOs; the resolution helper
`effective_watchpoints(tenant, as_of)` is the one read the beat and the web
surface share, so "what was effective when this finding fired" is the same
query in both places.

### 2. Why not `scoped_settings`

Rejected as the home for watchpoints and calibration overrides:
`scoped_settings` is deliberately not audit-triggered (secret hygiene), has
no historisation, and its row-per-field shape fits credentials, not
multi-column domain objects with per-family invariants. ADR-0112 is
untouched.

### 3. Overlay semantics for derived families (`saa`, `anlv`, `rss`)

- Subjects continue to be enumerated from the effective limit sets and
  `_KNOWN_TAGS`; overlay rows are matched by `subject_key`. A subject
  without an overlay row behaves exactly as today.
- `warn_threshold_pct` overrides the 90% default **for that subject** in
  both the monitor's live computation and the beat's edge classification
  (both already parameterise `warn_threshold_pct`; the per-subject value is
  resolved before the call). Bounds: `50 < warn_threshold_pct < 100`,
  validated in the repository and the route.
- `re_trigger_delta` overrides the family default for that subject.
- **Mute is visible, never silent.** A muted subject keeps its watch-state
  upserts and its live monitor row (with a `muted` tag and a group-header
  count, e.g. "2 muted"); only *finding creation* is suppressed.
- **A BREACH cannot be muted.** For `saa`/`anlv`, if live status is BREACH,
  the mute toggle is disabled in the UI **and** the beat ignores `muted`
  for breach-edge findings. Nervousness can be muted; rule violations
  cannot. This is enforced beat-side, not only UI-side.

### 4. Four defined signal families

Common contract: each family gets a pure producer under
`services/analytics/` (DB-free; the impure beat fetches inputs and calls
it), emits per-subject observations into the existing watch-state/delta
pipeline, and states its magnitude in **badness units** — a scalar where
larger is always worse — so ADR-0087's direction-agnostic delta arithmetic
(acknowledge on rising/re-trigger, reset on falling) is reused unchanged.
Internally all families keep the OK/WARN/BREACH status vocabulary (so
`edge_band_from_status` and the edge semantics are reused verbatim); the UI
renders signal families as **Calm / Approaching / Triggered** — "breach" is
regulatory language and stays reserved for quota families. WARN for a signal
family means "within the warn fraction of the trigger threshold", using the
same `warn_threshold_pct` machinery (default 90% of threshold).

| Family | Subject key | Watchpoint shape | Magnitude (badness unit) | Trigger |
|---|---|---|---|---|
| `price` | `price:{instrument_id}` | one watchpoint = one instrument; `drop_pct`, `window_days` | adverse (downward) move in pp over the window | move ≥ `drop_pct` |
| `fx` | `fx:{BASE}/{QUOTE}` | one watchpoint = one explicit pair; `move_pct`, `window_days` | absolute move in pp over the window (either direction — FX pain is book-dependent) | \|move\| ≥ `move_pct` |
| `freshness` | `freshness:{investment_id}` | **singleton** per tenant; `max_age_days` applies to all investments (subjects enumerated like quota subjects) | days the newest NAV exceeds `max_age_days` (0 if within) | age > `max_age_days` |
| `liquidity` | `liquidity:cash_coverage` | **singleton** per tenant; `horizon_months`, `min_coverage_ratio` | shortfall in coverage-ratio pp below `min_coverage_ratio` (0 if covered) | ratio < `min_coverage_ratio` |

- `price` v1 watches **declines only** (long-book assumption); direction
  configurability is a commissioned successor, not a hidden option.
- `liquidity` reads the **materialised** cash plan path (ADR-0103 §6) and
  the projected calls within the horizon; it computes a ratio, never
  re-materialises. If no plan path exists for the horizon, the subject
  reports a NO_DATA-style status in the monitor and produces no finding —
  absence of data is shown, never guessed over.
- `freshness` is deliberately a data-quality watcher: it demonstrates that
  the Watch Desk also watches the ground it stands on.

**Floor/cap calibration** (`FloorConfig` gains four trigger types; v1
defaults, refinable): `price_trigger` floor 4, `fx_trigger` floor 4,
`freshness_trigger` floor 3 **cap 5** (a stale NAV never outranks a
breach), `liquidity_trigger` floor 6. Source is `internal`, all-clear
falling edges reuse the existing `all_clear` semantics.

### 5. Per-tenant FloorConfig resolution

`DEFAULT_FLOOR_CONFIG` remains the code default. The beat composes the
effective config per run: defaults ⊕ the tenant's latest effective
`floor_calibration` row (§7) ⊕ per-subject overlay values (§3).
Composition re-runs the full `FloorConfig` validation **and** the pinned
invariants (§7), so a historical row that a later boundary edit would
invalidate can never silently produce an inverted configuration — the write
path prevents creating such a pair in the first place. Composition happens in the
impure beat; the pure layers keep receiving one `FloorConfig` plus resolved
per-subject values as plain arguments. The analytics purity guard is
unaffected.

### 6. Monitor: generalised honesty, new groups, edit affordances

- The two load-bearing honesty rules generalise, they do not weaken:
  **every gauge runs 0 → its trigger threshold with the mark fixed at the
  warn fraction; never rescale per row, never move the mark; a crossed
  threshold clamps the fill while the printed figures stay honest.** For
  quota groups this is byte-identical to today (threshold = ceiling, mark
  at the — now possibly per-subject — WARN percentage).
- Signal groups render family-appropriate columns: Subject / Status
  (Calm–Approaching–Triggered) / Value / Threshold / Proximity / Note. The
  note column stays route-assembled; the template composes no claims.
- Each row gains an edit affordance (opens the row's watchpoint editor);
  each defined-family group header gains **"+ Add watchpoint"**; overlay
  groups instead keep "manage in Limits" for ceilings and gain "adjust
  sensitivity" per row. Muted rows stay visible with a tag; group headers
  count them.

### 7. Calibration section becomes the editor

The Calibration section (Watch Desk) grows from fact display to the tenant
calibration editor, following the Back-Office SAA configuration pattern
(editable fields, explicit save bar with dirty state, server-validated):
tenant-wide WARN default, per-family re-trigger deltas, **band boundaries,
trigger-type floors, source/trigger caps, and the options gate**, plus the
watchpoint list (all families, with versions history per watchpoint
reachable from the row). The cadence panel remains unchanged within it.

**Pinned invariants (non-editable, rendered locked with their rationale).**
ADR-0088 already distinguishes fixed levels from calibration; the editor
makes that distinction visible and enforces it in the write path (mirroring
the existing `FloorConfig` constructor validation, which is reused
unchanged):

1. `fund_closure` floor = cap = 10 — a pinned level, not calibration.
2. `limit_breach` floor must lie **within the critical band**
   (≥ `band_boundaries[1] + 1`) — a regulatory breach can never render
   below critical. Editing the band boundaries revalidates this coupling.
3. `cap[SOURCE_RSS]` ≤ `band_boundaries[0]` — a standalone press cluster
   never outranks an internal finding (ADR-0087/0088 design promise).
4. `cap[all_clear]` ≤ `band_boundaries[0]` — an all-clear is never itself
   urgent.

Everything else — band cut points, the remaining floors and caps, the
options gate band, WARN default, per-family re-trigger deltas — is
per-tenant calibration, editable within the constructor's validity rules
(monotonic boundaries covering 1–10, floors ≤ caps, options gate a final
band).

**Storage.** Tenant calibration lives in a sibling historised table
`floor_calibration` (same migration `b033`; same pattern as §1: immutable
version rows by `effective_from`, RLS, audit-triggered; typed columns for
every field above). **An absent row means code defaults** — no seeding is
required, and the editor always displays the *effective* values with a
"default / customised" marker per field, so the demo tenant shows the
defaults without a materialised row. The beat resolves
defaults ⊕ latest effective calibration row ⊕ per-subject overlays (§3),
in that order (§5).

### 8. Seeding (v1 defaults, refinable)

On tenant bootstrap and for `minathena-capital`: `freshness` singleton at
`max_age_days = 120`; `liquidity` singleton at `horizon_months = 12`,
`min_coverage_ratio = 1.2`; one `fx` watchpoint per currency pair present
in the book at seed time (`move_pct = 3.0`, `window_days = 5`). `price`
watchpoints are **not** seeded for new tenants (per-instrument noise risk);
the demo tenant is seeded with `price` watchpoints on its ten ETFs
(`drop_pct = 5.0`, `window_days = 5`) so the release screenshots show the
family live.

## Non-goals (binding)

- No class-level or book-derived selectors for `price`/`fx` (commissioned).
- No `pacing:*` family before the TA engine exists (#023, Strand 3).
- No limit-set CRUD — the B5 remainder stays deferred and untouched.
- No per-user watchpoints (tenant scope only), no auto-mute, no
  Irene-initiated watchpoint changes of any kind.
- No editing of the pinned invariants (§7): the `fund_closure` level, the
  limit-breach-below-critical coupling, and the RSS / all-clear caps above
  the informational band are not tenant knobs under any framing.
- No RSS tag vocabulary editing (the closed `_KNOWN_TAGS` set stands).

## Commissions (recorded here, not designed here)

- **Roadmap: class-level `price` selectors** (a watchpoint spanning an
  asset class, auto-covering new instruments) — needs its own concept work
  on dynamic subject identity and history.
- **Roadmap: book-derived `fx` pair sets** ("all pairs in the book",
  auto-following) — same dynamic-subject concern.
- **Roadmap: `price` direction configurability** (up / both).
- **Roadmap candidate: `pacing:*` family** — strictly after #023/TA.

## Consequences (implementation inventory, prompt-planning grade)

Migration `b033` (+ seeding; both tables); `core/models/watchpoint.py` +
`core/models/floor_calibration.py`;
`core/repositories/watchpoint_repository.py` +
`core/repositories/floor_calibration_repository.py`; producers
`services/analytics/{price_watch,fx_watch,nav_freshness,cash_coverage_watch}.py`
(pure) + beat integration in `services/irene/beat.py`; `FloorConfig`
extension; monitor route/partial generalisation
(`web/routes/watch_desk.py`, `watch_desk_monitor.html`); Calibration editor
(route + partials + save bar JS); tests per producer (pure), repository,
routes (ASGI), and a replay-style fixture per family; docs
(`architecture.md`, `readme.md`, roadmap). Prompt cut: P2 registry, P3
overlay + editor, P4 price/fx, P5 freshness/liquidity, P6 monitor UI —
after P1 (ADR-0115 rename). Two commits per prompt; full suite + browser
walk as operator gates; the #052 flip follows this programme.

## Revision History

| Date | Author | Change |
|---|---|---|
| 2026-08-10 | PortfoliFLOW project owner + Claude | Drafted (Proposed) from the 2026-08-10 concept discussion. |
| 2026-08-10 | PortfoliFLOW project owner + Claude | Pre-acceptance revision: band boundaries, floors, caps, and the options gate become per-tenant editable in the Calibration editor, stored in the historised `floor_calibration` table; four pinned honesty invariants stay non-editable (§5/§7, Non-goals updated). |
| 2026-08-11 | PortfoliFLOW project owner + Claude | Accepted; index status updated. |
