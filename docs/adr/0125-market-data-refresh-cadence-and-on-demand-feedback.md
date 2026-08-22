# ADR-0125: Sub-Hourly Market-Data Refresh Cadence, Kind-Aware Fetching, and On-Demand Refresh Feedback

Status: Accepted (2026-08-22)
Date: 2026-08-22
Supersedes: nothing (ADR-0093 remains Accepted and unedited — its disabled-by-default seed is kept; this ADR changes only the seeded cadence value. ADR-0119 remains Accepted and unedited; this ADR extends the cadence vocabulary it defined and revokes its "market-data admin surface keeps `("daily",)`" statement by a decision of that surface's own, exactly as §1 of ADR-0119 foresaw.)
Related: ADR-0086 (tick/due split, "tick finer than finest cadence"), ADR-0091 (provider port, Yahoo adapter), ADR-0092 (Excel precedence, live write path), ADR-0093 (market-data schedule, on-demand trigger, seeding STD-03), ADR-0117 (built-in tick scheduler), ADR-0119 (cadence vocabulary v1, anchor semantics), ADR-0120 (Watch Desk post-enqueue polling), ADR-0121 (owner role, owner-gated surfaces)

## Context

Live market data is the most visible proof that PortfoliFLOW is alive. Today
the machinery for a recurring refresh exists end to end — `market_data_schedule`
(b022), the shared cadence arithmetic in `services/irene/scheduling.py`, the
per-tenant refresh core in `services/investments/live_refresh.py`, the shared
tick runner (ADR-0117 §2) and the in-process scheduler that calls it every
60 seconds — but four things keep it from being *perceived*:

1. **Vocabulary.** The market-data admin surface offers a single cadence,
   `daily`, and the shared vocabulary (`_CADENCE_INTERVAL_HOURS`) is defined in
   whole hours. A quarter-hourly refresh cannot be expressed.
2. **Seeded cadence.** STD-03 seeds `cadence = daily · preferred_hour = 6`.
   An owner who enables live data must also choose an interval; the one-click
   opt-in the disabled-by-default design implies does not exist.
3. **Fetch cost.** Every refresh fetches every ingestable kind the capability
   matrix routes — `nav_price` *and* `dividend` for Yahoo — per live-eligible
   investment. At a 15-minute cadence the dividend fetch is pure waste and
   doubles the provider call volume.
4. **Feedback and visibility.** "Refresh now" (`POST /api/market-data/refresh-now`)
   enqueues and reports "queued"; the page never learns that the refresh
   landed. The Watch Desk solved the identical problem in ADR-0120 with a
   self-terminating poll. Outside Admin nothing shows when the book's live
   prices were last refreshed.

Two things are deliberately **not** in this ADR's context: a refresh on
process start (assessed: at most 15 minutes of gained freshness; the first
tick after a long outage already refreshes a past-due schedule) and the
flipping of STD-03's `enabled = FALSE` default (assessed and kept — ADR-0093's
"no silent external fetching for a fresh tenant" stands).

## Decision

### §1 Vocabulary v2 — minute-granular steps

`_CADENCE_INTERVAL_HOURS: dict[str, int]` becomes a `timedelta`-valued map
(name to be chosen by the implementer; the old name is removed, not aliased):

```
daily · every_6h · every_3h · every_2h · hourly · every_30m · every_15m
```

Existing members keep their exact values; `every_30m` = 30 minutes,
`every_15m` = 15 minutes. Persisted lowercase, no DB constraint, validation
solely in `services/irene/scheduling.py` — all as in ADR-0119 §1.

**Anchor semantics are unchanged and sufficient.** ADR-0119 §2 defines the
candidate set as `anchor + k·step` on local wall-clock time, with the anchor
at `preferred_hour:00`. For a 15-minute step the candidates are therefore
`:00`, `:15`, `:30`, `:45` of every hour — the quarter-hour grid measured
from the full hour is a property of the existing arithmetic, not a new rule.
The anchor hour is practically inert for both new members (as `hourly`
already is); that is accepted.

The exception stays `IreneCadenceInvalid` and the module stays under
`services/irene/`. Both names are now misleading — the vocabulary is
domain-neutral and has been since ADR-0093 reused it — and their relocation
to `services/scheduler/` is registered as accepted debt for a successor ADR,
not bundled here.

### §2 Per-domain choice lists diverge

The Watch Desk router's `_CADENCE_CHOICES` is **unchanged**
(`daily · every_6h · every_3h · every_2h · hourly`): an Irene beat every
15 minutes is an LLM-cost decision the Watch Desk has not taken. A pinning
test asserts the Watch Desk tuple does not grow.

The market-data router's `_CADENCE_CHOICES` becomes
`every_15m · every_30m · hourly · daily` (finest-first, the way an interval
picker reads), with a label map on the Watch Desk pattern (`CADENCE_LABELS`):
"Every 15 minutes", "Every 30 minutes", "Every hour", "Daily". The panel
caption reads "Refresh interval". `every_5m` is explicitly not offered.

### §3 Seeded cadence

STD-03 becomes:

```
cadence = every_15m · preferred_hour = 0 · timezone = Europe/Berlin · enabled = FALSE
```

`enabled` stays FALSE (ADR-0093 unchanged). `preferred_hour = 0` is the
honest value for an inert anchor. Opting a tenant in is then one checkbox and
Save in Admin → Market Data.

The seed remains insert-if-absent. **No backfill, no data migration:** an
existing row cannot be told apart from one an owner deliberately left at
`daily`, so existing tenants (including the demo tenant) keep their row and
are switched in Admin. One line in the release notes.

### §4 Kind-aware fetching in the refresh core

`refresh_tenant_live_data` splits the ingestable kinds into two sets:

- **Price kinds** — `nav_price` — fetched on every run.
- **Daily kinds** — every other member of `_INGESTABLE_KINDS` — fetched only
  when `last_run_at is None or last_run_at.date() < now.date()` evaluated in
  UTC, i.e. on the first run of each UTC calendar day.

The rule is derived from the fields the tick already passes; no schema
change, no runner-interface change, no new parameter. The `TenantRefreshReport`
gains no field — the existing counters remain correct for whichever kinds
ran. The forced-provider test seam (`--provider`) follows the same split.

Consequence to state plainly: after a tenant's first run of the day the
window for the daily kinds is `[last_run_at.date(), today]`, as today; the
intraday runs only re-fetch the current day's `nav_price` bar, whose close
is the last traded price while the session is open and a repeated value
(`noop_live`) once it is closed. Overnight and weekend runs are therefore
near-free `noop_live` rounds — acceptable for this ADR; trading-hours
awareness is the named successor (roadmap, see Consequences).

### §5 Post-enqueue feedback in Admin

`POST /api/market-data/refresh-now` keeps its enqueue semantics
(`enqueue_due_now`, enabled-only, no provider work in the request). Its
confirmation partial gains the ADR-0120 poller, one-for-one:

- `GET /api/market-data/refresh/poll?since=<server enqueue instant>` — 204
  while `last_run_at < since`; 286 with the re-rendered market-data panel
  once `last_run_at >= since`; 286 + `HX-Reswap: none` on a malformed
  `since`, a missing schedule row, or `since` older than the 10-minute
  horizon. 15-second interval, starts only from the confirmation, never on
  load, replaced by the 286 swap so no second poller can survive.
- The poll endpoint reads exactly one schedule row on the pending branch.
- `require_session`, not `require_authenticated_session`, for the same
  reason ADR-0120 gives: a poll must not keep an abandoned session alive.

### §6 Freshness stamp and owner-gated refresh in the Front Office Overview

The Overview's existing `.ov-meta` line ("As of {date}") becomes the
freshness line of the book:

- **All members:** `As of {as_of_date} · Live data updated {HH:MM}` where the
  time is `market_data_schedule.last_run_at` rendered in the schedule's
  timezone. When the schedule is disabled or has never run, the second clause
  reads `Live data off` / `Live data not yet refreshed`.
- **Owners only** (`has_role("owner")`, the ADR-0121 gate as used by
  Providers & Credentials): when the schedule is enabled, a `Refresh`
  affordance follows the stamp; when disabled, `Enable in Admin` linking to
  `/admin#market-data`. Members see the stamp and nothing clickable.

The affordance posts to `/api/market-data/refresh-now` with a target inside
`#ov-section-body`; the response is a compact confirmation carrying the same
poller as §5, whose 286 swaps **`#ov-section-body` as `outerHTML`** — the full
Overview body, re-rendered from the refreshed prices. Partial updates (stamp
only, "reload to see") were considered and rejected: the reason for the
refresh is to see the numbers move.

Owner gating is enforced **server-side on the refresh-now route** as well as
in the template: a member who posts to the endpoint directly receives the
same 403 shape the other owner-gated routes return. This tightens the existing
Admin "Refresh now" too — Admin → Market Data is already an owner surface
under ADR-0121, so nothing observable changes there.

The `/api/overview/section` context gains the schedule's `enabled`,
`last_run_at`, `timezone` and the caller's owner flag. The Overview route
reads the schedule through `MarketDataScheduleRepository` — the same
tenant-scoped read Admin uses — and still imports neither the refresh core
nor any adapter (ADR-0093 verification gate extends to this route).

### §7 Rendering cost — bounded by construction

No page polls on cadence. The 15-minute refresh changes rows in the
database; a page learns about it on its next load or section reveal, exactly
as today. The only re-render this ADR adds is **one** Overview body render
per *manual* refresh, triggered by the single 286 that ends the poll — the
same cost as one reveal of the section. The pending polls (204) render
nothing and read one indexed row. Plotly targets inside the swapped body
re-initialise through the existing `renderTarget` guard (`data-pf-rendered`),
which the `outerHTML` swap resets because the nodes are new. Should the
Overview grow heavier, the lever is the Overview's own rendering, not this
feedback loop.

### §8 External tick source tightened

`docs/deploy/market-data-tick.timer` currently fires `OnCalendar=*:0/15`,
which would make the external tick exactly as coarse as the finest new
cadence and violate ADR-0086's "tick finer than finest cadence" condition
for opt-out deployments. The template becomes `OnCalendar=*:0/5`
(`RandomizedDelaySec` reduced to `15`), and `README-market-data-tick.md`
states the condition. The built-in scheduler's 60-second default (ADR-0117
§4) already satisfies it and is unchanged: a quarter-hour due instant is
noticed within the following minute, which this ADR documents as the
promised granularity ("within a minute of the quarter hour").

## Consequences

- **Vocabulary:** `_SUPPORTED_CADENCES` gains two members; the pinned error
  message changes; DST unit tests are added for `every_15m` across a
  spring-forward (the four candidates inside the gap collapse onto the first
  instant after it) and a fall-back (the repeated hour's four candidates fire
  once).
- **Refresh core:** kind split with tests for "first run of the day fetches
  all kinds", "second run fetches price kinds only", "never-run tenant
  fetches all kinds", and the forced-provider path.
- **Routes:** new poll endpoint with the four ADR-0120 branches tested;
  refresh-now gains an owner gate (403 for members, tested); Overview route
  gains schedule context (owner/member render tested, disabled/never-run
  copy tested). The ADR-0093 "web layer imports no provider" regression guard
  covers the Overview route.
- **Seed:** STD-03 test updated to `every_15m / 0 / disabled`; idempotency
  test unchanged.
- **Deploy docs:** timer template and README updated; operator handbook
  sentence on granularity.
- **No migration.** `cadence` is `TEXT` without a CHECK constraint.
- **Demo tenant:** the operator enables live data in Admin after deploy; no
  code path touches Minathena's existing row.
- **Roadmap:** two new items registered —
  - **#063 Market-data trading-hours awareness** (P2): skip intraday price
    fetches outside the instrument's exchange session and on weekends;
    requires an exchange-calendar source and a per-identifier session lookup;
    own concept decision.
  - **#064 Provider retry policy in the tick** (P2): ADR-0091 assigned retry
    policy to the tick job and no slice delivered it; bounded retries with
    backoff for `ProviderFetchError` (never for `IdentifierNotResolvableError`),
    per-investment, inside the existing failure isolation.
- **Accepted debt:** `IreneCadenceInvalid` and the `services/irene/`
  location of the domain-neutral vocabulary (successor ADR).

## Implementation strands

Each strand is one Claude Code prompt with its own verify-first phase,
not-in-scope block and commit. M1 precedes M2; M2, M3, M5 are independent of
one another.

| Strand | Scope | Commit type |
|---|---|---|
| **M1** | §1 vocabulary v2 (`timedelta` map, `every_15m`/`every_30m`), DST tests, Watch Desk pinning test, §8 timer template + README | `feat(scheduling)` |
| **M2** | §2 market-data choices + labels + caption, §3 seed value + STD-03 test | `feat(market-data)` |
| **M3** | §4 kind-aware fetching in `live_refresh.py` + tests | `feat(market-data)` |
| **M5** | §5 Admin poll endpoint + confirmation partial, §6 Overview freshness line + owner-gated refresh + server-side gate, §7 holds by construction, regression-guard extension | `feat(front-office)` |

(M4 — startup refresh — was assessed and dropped before acceptance; the
numbering gap is intentional so the discussion record stays traceable.)
