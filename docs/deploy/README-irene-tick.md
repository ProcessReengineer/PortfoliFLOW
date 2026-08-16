# Deploying the Irene heartbeat tick

The Irene heartbeat (ADR-0086) needs a **tick source**: something that
periodically asks "which tenants are due?". Since ADR-0117 there are two,
and the built-in one is the default.

- **[Default](#the-default-the-built-in-tick-scheduler)** — the built-in
  in-process scheduler. Nothing to install.
- **[Opt-out](#the-opt-out-running-the-tick-outside-the-web-process)** —
  the example systemd units in this directory, for running the tick
  outside the web process.

Both drive the same shared runner (`services/scheduler/tick_runner.py`,
ADR-0117 §2), so a tick behaves identically whichever source fires it.

## The default: the built-in tick scheduler

Starting `portfoliflow-web` starts one background task that sleeps a
fixed short interval and then runs one market-data tick and one Irene
tick (ADR-0117 §1). Anyone who starts the web app therefore has a working
heartbeat with **no OS-level setup at all** — no systemd, no cron, no
extra process to supervise.

Two environment variables configure it, in the application scope (the
same `.env` everything else reads — ADR-0117 §4):

- `TICK_SCHEDULER_ENABLED` — default `true`. Set `false` to run no
  in-process task and use the opt-out below instead.
- `TICK_SCHEDULER_INTERVAL_SECONDS` — default `60`, bounds-checked to
  `[5, 3600]`. An out-of-range value fails at startup rather than being
  silently clamped.

Deliberately **not** configurable in the UI or the database: which tick
source runs is process topology, not domain data (ADR-0117 §4).

**Tick ≠ cadence.** The interval above is only how often "who is due?" is
asked. *When* a tenant is due is per-tenant data in `irene_schedule`,
edited in the Watch Desk cadence panel, and no tick-source setting
changes it. That is ADR-0086's tick/due split, and ADR-0117 is exactly
the swap it was drawn for.

**Checking on it.** `GET /health` reports a `tick_scheduler` object — the
mode (`internal` / `external`), whether the task is alive, and the
timestamp of the last completed tick. Super Admin → Platform shows the
same three facts, human-readable (ADR-0117 §5). Per-tenant evidence stays
where it was: `last_beat_at` / "Next beat due" in the cadence panel.

## The opt-out: running the tick outside the web process

The rest of this document is the **opt-out**: `irene-tick.service` and
`irene-tick.timer` in this directory are **example** systemd units that
run `portfoliflow irene-tick` from outside the web process. They are
templates the operator installs and edits by hand — nothing in the repo
installs or enables them automatically.

Reasons to choose it over the default:

- **Resource isolation** — LLM synthesis and provider I/O run in their
  own short-lived process instead of the uvicorn event loop.
- **Per-run exit codes** — a compliance or monitoring setup that wants a
  non-zero exit per failed run gets one (see [Exit codes](#exit-codes));
  the in-process scheduler replaces that with the health endpoint's
  last-tick timestamp and the per-tenant schedule rows.
- **Multi-worker futures** — a deployment that one day drops the
  single-worker constraint keeps one tick source rather than one per
  worker.

Set `TICK_SCHEDULER_ENABLED=false` in the deployment environment, restart
`portfoliflow-web`, then install the units as described below.

**Coexistence is safe by design.** The internal scheduler and an external
timer may run at the same time: every beat is claimed with
`pg_try_advisory_xact_lock`, so whichever tick source gets there first
beats a tenant and the other skips. Migration in either direction — and
in either order — is therefore risk-free, and an overlap while you switch
costs nothing (ADR-0117 §4).

## What the tick does

`portfoliflow irene-tick` is a **tenant-blind, one-shot** command. On each
run it:

1. reads AI credentials from `.env` (no key ⇒ it exits 0, a no-op);
2. finds every tenant whose `irene_schedule.next_due_at <= now()` and is
   `enabled` (a cross-tenant read on the superuser engine);
3. for each due tenant, claims the beat with a Postgres advisory lock,
   runs one synthesis beat inside a tenant-scoped transaction, and
   advances that tenant's `next_due_at` from its cadence.

Per-tenant cadence lives in the database (`irene_schedule`), **not** in
the timer. The timer is a dumb fixed interval; the domain decides who is
actually due.

## Files

- `irene-tick.service` — a `Type=oneshot` unit that runs
  `portfoliflow irene-tick`.
- `irene-tick.timer` — fires the service every 15 minutes
  (`OnCalendar=*:0/15`). Fifteen minutes comfortably honours v0's finest
  cadence (daily at a preferred hour) and a tick on an empty due-set is
  near-free.

## Install (operator)

1. Set `TICK_SCHEDULER_ENABLED=false` in the deployment environment and
   restart `portfoliflow-web`, unless you deliberately want both tick
   sources running (which is safe — see the coexistence note above).
2. Edit **`irene-tick.service`** for your deployment — at minimum:
   - `User` / `Group` (never root);
   - `WorkingDirectory` (the install/repo root, so the CLI finds `.env`
     and the Alembic tree);
   - `ExecStart` (the absolute path to the venv's `portfoliflow` script).
   Optionally set `EnvironmentFile=` instead of relying on the repo `.env`.
3. Copy both units into `/etc/systemd/system/` (or a user unit dir).
4. `sudo systemctl daemon-reload`
5. `sudo systemctl enable --now irene-tick.timer`
6. Check it: `systemctl list-timers irene-tick.timer` and
   `journalctl -u irene-tick.service`.

## Environment

The tick reads the same `.env` the web app and bot read:

- `DATABASE_URL_SUPERUSER` — required (the cross-tenant due read runs on
  the superuser engine).
- `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL` — the LLM endpoint. With no
  key the tick logs a warning and exits 0.
- `IRENE_MODEL` — the synthesis model for Irene. Falls back to
  `SHIRLEY_MODEL`, then to a built-in default. Irene is deliberately not
  pinned to Shirley's model. (Also listed in `.env.example`.)

**Not environment variables — by design.** The RSS clustering parameters
— the pinned `embedding_model` (`openai/text-embedding-3-small`) and the
`similarity_threshold` (`0.83`) — are **code constants** in
`services/analytics/irene_floor.py` (`FloorConfig`), not `.env` keys. This
is intentional (ADR-0087): they are *auditable calibration*, and changing
the embedding model **freezes existing open RSS buckets** (the bucket key
hashes membership, not the vector, so a model change can neither re-form
nor re-key a frozen `rss:cluster:*` subject). A tracked change to a
versioned constant is auditable in a way a silent `.env` edit is not — so
these live in code on purpose. The same holds for the deterministic urgency
floors, caps, and band boundaries in `FloorConfig`. To recalibrate, edit
`FloorConfig` and commit the change (a Watchlist edit surface for the
thresholds is a planned v1 addition).

## Exit codes

Exit codes are a property of the CLI tick, i.e. of the opt-out path; the
built-in scheduler logs and retries on the next interval instead.

- `0` — success, including "nothing due" and "no API key". A single
  tenant's beat failure is logged and isolated; it does not fail the tick.
- `2` — configuration error (e.g. `DATABASE_URL_SUPERUSER` unset).
- `3` — another PortfoliFLOW error.

A non-zero exit therefore means the **tick itself** failed (typically the
database was unreachable), not that one tenant's beat had a problem.
