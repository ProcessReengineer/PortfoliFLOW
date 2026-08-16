# ADR-0117: Built-in Tick Scheduler — In-Process Default with External Opt-out

- **Status:** Accepted (2026-08-11)
- **Date:** 2026-08-11
- **Deciders:** PortfoliFLOW project owner
- **Implements roadmap item:** #058 (Built-in Tick Scheduler)
- **Tags:** watch-desk, irene, market-data, scheduling, deployment, operations, multi-tenancy

---

## Context

ADR-0086 split the heartbeat into a dumb, tenant-blind **tick source** and a
database-driven **due evaluation**, and shipped v0 with an external systemd
timer invoking `cli/irene_tick.py`. ADR-0093 reused the topology 1:1 for the
market-data live-import tick (`cli/market_data_tick.py`, disjoint advisory-lock
domain). Both ADRs documented the tick source as an explicitly swappable seam:
"replaceable (e.g. an in-process loop) without touching domain logic."

The systemd default has three deployment problems:

1. **Availability.** Not every target system runs systemd (containers without
   an init system, macOS, BSDs, minimal images).
2. **Willingness and ability.** Even where systemd exists, requiring an
   operator to install and enable two timer/service unit pairs is a real
   adoption barrier — precisely the audience an AGPL self-host release wants
   to serve includes people who will not (or may not) touch `/etc/systemd`.
3. **SaaS cadence.** *Already solved* by ADR-0085/0086 and requires no change:
   cadence is per-tenant data (`irene_schedule`, `market_data_schedule`), the
   tick is tenant-blind, and two tenants with different rhythms are two rows.
   This ADR changes only *what ticks*, never *who is due when*.

ADR-0086 rejected the in-process loop as the v0 default on three grounds. All
three have since been resolved or were never applicable to the shape that
shipped:

- **Multi-worker fan-out** ("the loop would fire once per worker"). Moot:
  single-worker uvicorn is now a load-bearing deployment constraint N times
  over — the Telegram multi-bot multiplexing (ADR-0112 §5), `pending_turns`,
  and the process-wide `_TURN_LOCK` all assume it, as documented in
  `web/main.py` and `docs/deploy/telegram-multi-bot.md`. And even under a
  misconfigured multi-worker start, `pg_try_advisory_xact_lock` makes
  concurrent ticks harmless by construction: the second claimant skips.
- **`_TURN_LOCK` sharing.** Never applicable to the shipped shape:
  `run_synthesis` (ADR-0086/0088) is non-streaming and lock-free by design;
  a beat shares no lock with Shirley regardless of which process runs it.
- **uvicorn lifecycle coupling.** The one real point, and it is bounded: a
  beat runs inside an advisory-lock transaction. A process restart mid-beat
  rolls back atomically, `next_due_at` remains unadvanced, and the next tick
  retries. No beat is lost, none double-runs.

What remains genuinely open is where the cross-tenant due read gets its
RLS-bypassing connection inside the web process, how the CLI ticks and the
in-process scheduler share one implementation, and how the tick source is
configured and made visible.

## Decision

### 1. An in-process tick task becomes the default tick source

The web lifespan (`web/main.py`) starts one asyncio background task — the
**built-in tick scheduler** — alongside the existing long-lived background
work (the Telegram bot). The task loops: sleep a fixed short interval
(default **60 seconds**), then run one tick for the **irene** domain and one
for the **market_data** domain, sequentially, using exactly the pipeline the
CLI ticks use today (cross-tenant due read → per-tenant `tenant_context` →
`pg_try_advisory_xact_lock` as the first statement → beat/refresh →
schedule advance, all in one transaction per tenant, per-tenant failure
isolation). In the common case nothing is due and a tick is one cheap
`SELECT` per domain.

Anyone who starts `portfoliflow-web` therefore has a working heartbeat with
zero OS-level configuration. This is the answer to Context problems 1 and 2.

The task is cancelled on lifespan shutdown. An in-flight beat either
completes within a short grace period or is cancelled; cancellation rolls the
advisory-lock transaction back, so the beat is retried after restart (see
Context — lifecycle coupling).

### 2. One shared tick runner; the CLIs become thin wrappers

The per-tick orchestration currently living in `cli/irene_tick.py::_run()`
and `cli/market_data_tick.py::_run()` (due read, lock claim, per-tenant
credential resolution where applicable, beat/refresh, advance, failure
isolation, structured logging) is extracted into a shared, **engine-
parametrised** runner (working name: `services/scheduler/tick_runner.py` —
one module, two entry points, one per domain). Callers supply the
RLS-bypassing connection source:

- the CLI ticks pass `cli/_db.superuser_engine` (unchanged behaviour,
  unchanged exit-code semantics, unchanged systemd units);
- the in-process scheduler passes the web app's audit engine (§3).

Neither implementation may drift from the other; the CLIs keep their
test-seam flags (`--tenant`, `--provider`, ADR-0093 §0.4) as wrapper-level
concerns.

The RSS harvest (`_harvest_rss_items`, synchronous by nature) moves into the
shared runner and is invoked via `asyncio.to_thread`, so it never blocks the
uvicorn event loop. Its tolerance contract is unchanged: any failure degrades
to an internal-only beat, never a tick error.

### 3. The scheduler's due reads become the fifth sanctioned audit-engine path

The cross-tenant due reads (`irene_schedule`, `market_data_schedule` +
its join against live-eligible investments) need an RLS-bypassing connection
inside the web process. Rather than constructing a third engine on the same
superuser URL (pure pool overhead), the scheduler uses
`app.state.audit_engine`, and this usage is added as **path 5** to the
sanctioned-paths regression guard
(`tests/regression/test_audit_engine_only_writes_login_audit.py`), following
the precedent of path 4 (ADR-0112 §5, the Telegram bot-token scan): a
platform-level read that spans tenants and therefore runs before any tenant
context exists.

The guard asserts path 5 is **read-only** and touches **only** the two
schedule tables. The runner receives **one** engine and carries the
per-tenant transactions on it as well: every tenant-scoped statement inside
a beat — the advisory-lock claim, the beat's writes, and the
schedule-advance write, all in the one transaction — runs inside
`tenant_context(enforce_rls=True)`, which switches the session to the
unprivileged `APP_DB_ROLE` for the remainder of that transaction (ADR-0078),
so RLS is enforced regardless of the role the engine connects as. The
**superuser-privileged surface** of path 5 is therefore confined to the
cross-tenant due reads on the two schedule tables — exactly the mechanism
the CLI ticks' superuser engine relies on today.

### 4. The tick source is deployment-scope configuration — environment, not database

Which tick source runs is process topology, not domain data. It is configured
by environment variables in the application scope (the same scope position
`.env` holds in the ADR-0112 credential chain), deliberately **not** editable
in the UI or stored in the database:

- `TICK_SCHEDULER_ENABLED` — default `true`. `false` disables the in-process
  task; the operator runs the CLI ticks via systemd/cron/etc. (the existing
  units under `docs/deploy/` remain the documented external path).
- `TICK_SCHEDULER_INTERVAL_SECONDS` — default `60`. Bounds-checked; this is
  the *tick* interval (how often "who is due?" is asked), never a cadence.

A database-resident toggle was rejected: read at startup it lies in the UI
("switched, but effective only after restart"); hot-switchable it adds
complexity for no user value. Per-tenant cadence remains the only
tenant-facing scheduling configuration, unchanged in the Watch Desk cadence
panel.

**Coexistence is safe by design:** internal scheduler and external timers may
run simultaneously — the advisory locks deduplicate, whichever claimant fires
first beats, the other skips. Migration of an existing systemd deployment is
therefore risk-free in either order.

### 5. Visibility instead of editability in the UI

What the UI gains is the scheduler's *health*, not its configuration:

- **Health endpoint:** reports the scheduler mode (`internal` / `external`),
  whether the task is alive, and the timestamp of the last completed tick.
- **Super Admin:** the same three facts, human-readable, on the existing
  platform-status surface.
- **Watch Desk cadence panel:** unchanged — "Next beat due" already shows the
  tenant-facing consequence; `last_beat_at` remains the per-tenant evidence
  that the heartbeat lives.

No new tenant-facing controls are introduced by this ADR.

## Consequences

- A fresh install has a working Irene heartbeat and market-data refresh the
  moment `portfoliflow-web` starts; systemd knowledge is no longer required
  for the default experience. The systemd units are demoted from "the v0
  deployment" to a documented opt-out (`README-irene-tick.md`,
  `README-market-data-tick.md` gain a paragraph; the units themselves are
  untouched).
- The web process now hosts LLM batch work (Irene's synthesis call) and
  provider I/O (market-data refresh). Both are awaited I/O on the event loop;
  the one synchronous component (RSS harvest) is threaded (§2). The
  single-worker constraint gains one more dependent — acceptable, since it is
  already load-bearing for the bot and the turn lock.
- ADR-0086 and ADR-0093 remain immutable and correct: this ADR *uses* the
  seam they drew, it does not revise them. Their "Alternatives Considered"
  rejection of the in-process loop was a v0 default choice whose premises
  have since changed (Context); this ADR is the successor record for that
  choice.
- The regression guard grows a fifth path with a strictly read-only,
  two-table surface; the audit engine's asymmetry documentation in
  `web/main.py` is updated accordingly.
- `cli/irene_tick.py` and `cli/market_data_tick.py` shrink to wrappers; their
  tests largely move to the shared runner, keeping wrapper tests for exit
  codes and flags.

## Alternatives Considered

- **Separate long-lived scheduler process** (`portfoliflow scheduler`, e.g. a
  second Compose service). Cleaner isolation, but it merely relocates the
  adoption barrier from systemd to Compose/supervisord/tmux: whoever does not
  deploy via Compose must again start and babysit an extra process. Fails
  Context problem 2, which is the point of this ADR. Remains possible later
  behind the same runner seam (§2) if resource isolation ever demands it.
- **Browser-driven ticks** (a due check piggybacked on HTMX polling while any
  user session is open). Rejected outright: the heartbeat's entire value is
  proactivity with nobody watching; a pulse that stops when the last tab
  closes is not a pulse.
- **APScheduler or similar.** Rejected again for the ADR-0086 reasons: a
  framework dependency for "sleep, then one query" is disproportionate, and
  the in-process variant would inherit exactly the questions this ADR already
  answers without it.
- **Database-editable tick-source toggle.** Rejected (§4): topology
  configuration read at startup misrepresents itself in a UI; per-tenant
  cadence is the correct — and existing — tenant-facing knob.
- **A dedicated third superuser engine in the web process.** Rejected (§3):
  same URL, same privileges, extra pool — sanctioning a read-only path on the
  existing audit engine with a structural guard is strictly simpler and
  keeps all RLS-bypassing usage enumerable in one test.

## Compliance & Audit Relevance

- **Operational transparency (BAIT/VAIT):** the shared runner emits the same
  structured log lines per tick and per tenant beat in both hosts; the CLI
  path additionally keeps its exit codes. What the internal scheduler loses
  in exit codes it replaces with persistent, queryable evidence: the health
  endpoint's last-tick timestamp and the per-tenant `last_beat_at` /
  `next_due_at` rows — "did the beat run, for which tenants, with what
  outcome" remains fully inspectable without shell access.
- **Determinism & reproducibility:** unchanged from ADR-0086 — cadence and
  due state are persisted; the tick source (internal or external) never
  influences *when* a tenant is due, only *that* due tenants are found.
- **Isolation (DORA):** unchanged — advisory-lock claiming plus per-tenant
  `tenant_context` prevent cross-tenant interference and double execution,
  now additionally proven against the internal/external coexistence case.
- **Least privilege:** the RLS-bypassing surface of the web process remains
  enumerated and structurally guarded; path 5 is read-only over two schedule
  tables and adds no superuser write capability.

## Revision History

- 2026-08-11 — Proposed.
- 2026-08-11 — Roadmap reference corrected `#057` → `#058`: the originally
  cited id had already been absorbed by the Watchpoint Registry programme
  (ADR-0116) when this ADR was filed, so the roadmap entry was issued at the
  genuinely next-free id and the header now cites it.
- 2026-08-11 — §3 precision fix against the delivered runner shape
  (`services/scheduler/tick_runner.py`): the runner takes **one** engine and
  carries the per-tenant transactions on it too, so the superuser-privileged
  surface is the cross-tenant due reads alone — `tenant_context` drops to the
  unprivileged `APP_DB_ROLE` inside each tenant transaction (ADR-0078).
  Supersedes the earlier "runs on the app engine / the audit engine never
  executes a beat" wording.
- 2026-08-11 — Accepted.
