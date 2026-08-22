# Deploying the market-data live-import tick

The market-data live-import tick (ADR-0093) needs a **tick source**:
something that periodically asks "which tenants are due?". Since ADR-0117
there are two, and the built-in one is the default.

- **[Default](#the-default-the-built-in-tick-scheduler)** — the built-in
  in-process scheduler. Nothing to install.
- **[Opt-out](#the-opt-out-running-the-tick-outside-the-web-process)** —
  the example systemd units in this directory, for running the tick
  outside the web process.

The pattern is identical to `irene-tick` (ADR-0086) in both modes — one
scheduler task and one shared runner serve both domains (ADR-0117 §1, §2),
so an operator who has deployed that one already knows this one.

## The default: the built-in tick scheduler

Starting `portfoliflow-web` starts one background task that sleeps a
fixed short interval and then runs one market-data tick and one Irene
tick (ADR-0117 §1). Anyone who starts the web app therefore gets live
market data with **no OS-level setup at all** — no systemd, no cron, no
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
asked. *When* a tenant is due is per-tenant data in
`market_data_schedule`, edited at `/admin#market-data`, and no tick-source
setting changes it. A freshly provisioned tenant is still **disabled** by
default (see [Enabling a tenant](#enabling-a-tenant)) — the built-in
scheduler changes what ticks, never who has opted in.

The one condition binding the two is ADR-0086's: the tick interval must be
**strictly finer than the finest cadence a tenant can choose**, which since
ADR-0125 §1 is `every_15m`. The 60-second default above satisfies it with
room to spare — a quarter-hour due instant is noticed within the following
minute — and the external timer template was tightened to 5 minutes for the
same reason (ADR-0125 §8).

**Checking on it.** `GET /health` reports a `tick_scheduler` object — the
mode (`internal` / `external`), whether the task is alive, and the
timestamp of the last completed tick. Super Admin → Platform shows the
same three facts, human-readable (ADR-0117 §5). Per-tenant evidence stays
where it was: `last_run_at` / next-due in the market-data panel.

## The opt-out: running the tick outside the web process

The rest of this document is the **opt-out**:
`market-data-tick.service` and `market-data-tick.timer` in this directory
are **example** systemd units that run `portfoliflow market-data-tick`
from outside the web process. They are templates the operator installs and
edits by hand — nothing in the repo installs or enables them
automatically.

Reasons to choose it over the default:

- **Resource isolation** — provider I/O runs in its own short-lived
  process instead of the uvicorn event loop.
- **Per-run exit codes** — a compliance or monitoring setup that wants a
  non-zero exit per failed run gets one (see [Exit codes](#exit-codes));
  the in-process scheduler replaces that with the health endpoint's
  last-tick timestamp and the per-tenant schedule rows.
- **Multi-worker futures** — a deployment that one day drops the
  single-worker constraint keeps one tick source rather than one per
  worker.

Set `TICK_SCHEDULER_ENABLED=false` in the deployment environment, restart
`portfoliflow-web`, then install the units as described below. Note the
switch is per *process*, not per domain: it turns both the market-data
and the Irene tick off in the web process, so install both unit pairs.

**Coexistence is safe by design.** The internal scheduler and an external
timer may run at the same time: every refresh is claimed with
`pg_try_advisory_xact_lock` on the `market_data`-domain key, so whichever
tick source gets there first refreshes a tenant and the other skips.
Migration in either direction — and in either order — is therefore
risk-free, and an overlap while you switch costs nothing (ADR-0117 §4).

## What the tick does

`portfoliflow market-data-tick` is a **tenant-blind, one-shot** command
with **no AI dependency**. On each run it:

1. finds every tenant whose `market_data_schedule.next_due_at <= now()` and
   is `enabled` (a cross-tenant read on the superuser engine);
2. for each due tenant, claims the refresh with a Postgres advisory lock
   (on a `market_data`-domain key, disjoint from Irene's beat lock), runs
   the refresh core inside a tenant-scoped transaction, and advances that
   tenant's `next_due_at` from its cadence.

The refresh core resolves each tenant's **live-eligible** investments
(`listed_equity` / `listed_bonds` with a primary ISIN / ticker / FIGI —
ADR-0090), fetches NAV/price and cashflow series from the routed provider
(ADR-0091), and writes them under the Excel-precedence guard (ADR-0092),
attributed to the tenant's seeded market-data system actor. A private-
markets position is skipped cleanly; one investment's provider failure is
logged and contained; one tenant's failure never aborts the tick.

Per-tenant cadence lives in the database (`market_data_schedule`), **not**
in the timer. The timer is a dumb fixed interval; the domain decides who is
actually due.

## Files

- `market-data-tick.service` — a `Type=oneshot` unit that runs
  `portfoliflow market-data-tick`.
- `market-data-tick.timer` — fires the service every 5 minutes
  (`OnCalendar=*:0/5`). Five minutes stays strictly finer than the finest
  cadence the surface offers (`every_15m`, ADR-0125 §1), so each refresh
  lands within 5 minutes of its quarter-hour slot, and a tick on an empty
  due-set is near-free.

## Install (operator)

1. Set `TICK_SCHEDULER_ENABLED=false` in the deployment environment and
   restart `portfoliflow-web`, unless you deliberately want both tick
   sources running (which is safe — see the coexistence note above).
2. Edit **`market-data-tick.service`** for your deployment — at minimum:
   - `User` / `Group` (never root);
   - `WorkingDirectory` (the install/repo root, so the CLI finds `.env`);
   - `ExecStart` (the absolute path to the venv's `portfoliflow` script).
   Optionally set `EnvironmentFile=` instead of relying on the repo `.env`.
3. Copy both units into `/etc/systemd/system/` (or a user unit dir).
4. `sudo systemctl daemon-reload`
5. `sudo systemctl enable --now market-data-tick.timer`
6. Check it: `systemctl list-timers market-data-tick.timer` and
   `journalctl -u market-data-tick.service`.

## Environment

The tick reads the same `.env` the web app reads:

- `DATABASE_URL_SUPERUSER` — required (the cross-tenant due read runs on the
  superuser engine).
- `OPENFIGI_API_KEY` — optional; raises OpenFIGI's anonymous rate limit for
  ISIN → FIGI resolution. Yahoo needs no credentials.
- `MARKET_DATA_SYNTHETIC_FIXTURE` — **test sessions only** (see below).

## Bloomberg (Desktop API)

The Bloomberg adapter ships **fixture-validated but disabled**
(`enabled: false` in `config/market_data_capabilities.yaml`). A disabled
provider is skipped entirely by the factory, so nothing routes to it until
you flip the flag. Flipping `enabled: true` requires **all** of:

1. a locally running, entitled **Bloomberg Terminal** on the same host as
   the tick (the Desktop API listens on `localhost:8194` by default) —
   note that with the built-in scheduler the tick runs on the *web app's*
   host, which is therefore the host that needs the Terminal;
2. the **`blpapi`** package installed — it is **not** on public PyPI:
   `pip install --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi`;
3. the connection env vars if you are not on the defaults — `BLPAPI_HOST`
   (default `localhost`) and `BLPAPI_PORT` (default `8194`).

Bloomberg serves `figi` / `isin`-primary investments (security topics
`/bbgid/<FIGI>` and `/isin/<ISIN>`); it does **not** serve plain tickers
(that needs a yellow-key suffix the identifier model does not store), so it
extends coverage to ISIN/FIGI positions rather than competing with Yahoo.
The Desktop API declares `credentials: none` — the running Terminal session
is the auth/entitlement boundary. The credentialed Server-API / B-PIPE /
Data-License variants are **#037-gated** (per-tenant licensed) and are
separate future adapters, not a mode of this one.

**Activation order matters.** Flip `enabled: true` in the matrix **first** —
a disabled provider is dropped from routing entirely, and the `--provider`
force flag still honours the matrix's coverage declaration, so
`--provider bloomberg` serves nothing while the entry is disabled. Only after
flipping the flag will a forced run route to Bloomberg.

**First activation should then be a `--tenant`-scoped manual tick run while
watching the log** — the live smoke — before any tick source relies on it:

```console
BLPAPI_HOST=localhost BLPAPI_PORT=8194 \
  portfoliflow market-data-tick --tenant minathena-capital --provider bloomberg
```

Confirm NAV rows land with `source='bloomberg'` and no error lines, then
(and only then) leave routing to the scheduler or timer. If `blpapi` is
missing the fetch fails fast with a `MarketDataConfigurationError` naming
the install index.

## Enabling a tenant

A freshly provisioned tenant carries a `market_data_schedule` row that is
**disabled** by default (ADR-0093): no tenant silently starts fetching. An
owner opts in from the Admin surface (`/admin#market-data`): set the cadence
and tick the "Enabled" box. "Refresh now" there sets the schedule due
immediately (`next_due_at := now`) so the next tick picks the tenant up — it
runs no provider work in the request.

## Test session (synthetic provider, no live network)

The `--tenant` and `--provider` flags are a **test seam** (ADR-0093 §0.4).
Neither persists schedule state, so a test run never perturbs production
cadence, and no production tick source passes either — the built-in
scheduler drives the plain due-read path only (ADR-0117 §3):

- `--tenant <id-or-subdomain>` restricts the tick to one tenant and bypasses
  the due gate (still honouring the advisory lock).
- `--provider <name>` forces the factory to a named provider from the
  capability matrix — the documented way to point a run at `synthetic`.

Point a run at the synthetic test-event provider (its fixture path comes
from `MARKET_DATA_SYNTHETIC_FIXTURE`; see
`config/market_data_synthetic_example.json`):

```console
MARKET_DATA_SYNTHETIC_FIXTURE=/opt/portfoliflow/config/market_data_synthetic_example.json \
  portfoliflow market-data-tick --tenant minathena-capital --provider synthetic
```

This injects deterministic NAV/cashflow deltas onto the tenant's live-
eligible investments. A subsequent `portfoliflow irene-tick` then lets Irene
react to those deltas (they surface in the Watch Desk) — the operator's
end-to-end test loop: **market-data-tick → irene-tick → Watch Desk**. The
CLI stays the tool for this even on a default deployment: it runs both
domains on demand instead of waiting for the scheduler's next interval.

## Exit codes

Exit codes are a property of the CLI tick, i.e. of the opt-out path; the
built-in scheduler logs and retries on the next interval instead.

- `0` — success, including "nothing due". A single tenant's refresh failure
  is logged and isolated; it does not fail the tick.
- `2` — configuration error (e.g. `DATABASE_URL_SUPERUSER` unset).
- `3` — another PortfoliFLOW error.

A non-zero exit therefore means the **tick itself** failed (typically the
database was unreachable), not that one tenant's refresh had a problem.
