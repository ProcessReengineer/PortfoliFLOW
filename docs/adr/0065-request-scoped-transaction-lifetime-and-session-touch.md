# ADR-0065: Request-Scoped Transaction Lifetime and Session-Touch Placement

- **Status:** Accepted
- **Implemented:** 2026-05-28
- **Date:** 2026-05-28
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, database, concurrency, authentication, session-management, performance, multi-tenant

---

## Context

ADR-0035 committed PortfoliFLOW to Postgres Row-Level Security for
tenant isolation, mediated by the `tenant_context` async context
manager in `core/repositories/_session.py`. ADR-0036 introduced
server-side sessions with an idle-timeout that is reset by bumping
`sessions.last_seen_at` on every authenticated request — the
`SessionRepository.touch` write. ADR-0063 activated multi-tenancy and
added the role model (`require_role`), whose dependency chain
(`require_role` → `get_authenticated_user` → `get_authenticated_session`)
gates every mutating route.

A deterministic, data-independent hang was observed on the Excel-upload
route. Long authenticated requests (`POST /api/data-import/section/upload`)
never return; the browser shows `(pending)` indefinitely. Diagnosis via
`pg_stat_activity` during a hang:

```
pid | state               | wait_event_type | wait_event   | query
211 | idle in transaction | Client          | ClientRead   | SELECT ... FROM users ...
245 | active              | Lock            | transactionid| UPDATE sessions SET last_seen_at = NOW() WHERE id = $1
```

The hang is **not** data-related (two distinct test workbooks reproduce
it) and **not** corruption (a fresh `db-reset.sh` reproduces it
immediately). It is a structural property of how request-scoped
database sessions are wired.

### The verified mechanism — a single-request self-block

`get_authenticated_session` (`web/auth.py`) is a FastAPI
**yield-dependency**:

```python
async def get_authenticated_session(request, session=Depends(require_session)):
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch(session.id)   # row-lock on sessions
        yield db_session                                        # held until the request ENDS
```

Because it `yield`s, FastAPI keeps this `tenant_context` — call it
**T1** — open for the entire request. `touch()` issues
`UPDATE sessions SET last_seen_at = NOW()` and does not commit; the
commit only happens when T1 exits, i.e. after the whole handler
finishes. T1 therefore holds a row-lock on the session row, and an
`idle in transaction` connection-pool slot, for the request's full
lifetime.

The upload route is gated by `dependencies=[Depends(require_role("owner"))]`,
which pulls T1 in through the dependency chain. **The handler body does
not use the yielded T1 session at all** — it opens its own
`tenant_context` blocks for the actual work. Inside one of them,
`_render_preview_body` (`web/routes/data_import.py`) opens a second
`tenant_context` — **T2** — and calls `touch()` on the *same* session
row. T2 blocks on T1's lock. T1 cannot commit until the request ends;
the request cannot end because T2 is blocked. Postgres does not abort
this as a deadlock, because T1 is not waiting on a database lock — it
is waiting on `ClientRead` (the application). So it hangs forever
rather than being killed.

This matches the evidence exactly: PID 211 is T1 (`idle in transaction`,
its last statement the `SELECT ... FROM users` issued by
`get_authenticated_user`), PID 245 is T2 (`UPDATE sessions`, blocked).

### The wider structural finding

The self-block is one route. The underlying structure is systemic:

- **~25 routes** depend on `require_role` (investments: 10, saa_section:
  7, chat: 2, scraper: 2, ai_settings: 1, portfolio_analysis: 1,
  data_import: 2). Every one inherits T1 and holds it
  `idle in transaction` — with a session-row lock and a pool slot —
  for the request's full duration, even though none of them uses the
  yielded session: they all open their own `tenant_context` for the
  work.
- Most of those handlers do **not** `touch()` inside their own work
  transaction (investments / saa_section / chat / scraper / ai_settings
  have zero internal `touch` calls), so they do **not** self-block. But
  T1 is pure overhead on each: a lock that secures nothing and a pool
  slot held across non-DB work.
- The "Pattern B" GET routes (charts, statistics, areas,
  portfolio_review, limits, benchmarks_attribution) depend only on
  `require_session` (a DTO, no DB), then open one `tenant_context`,
  `touch()` inside it, do their work, and commit. Single-request: this
  is harmless. Under concurrency, each holds the session-row lock for
  its own work-transaction duration — bounded waits that consume pool
  slots when the same session fires parallel fragment requests
  (the HTMX dashboard issues several at once).

The common root cause is not `touch()`. It is that **a write-capable
transaction is opened for the request rather than for the unit of
work**, and — in the dependency's case — held open across work it does
not participate in. `touch()` is merely the write that made the latent
lock-hold visible by colliding with itself on the upload route.

### Verified invariants (inputs to the decision)

- `tenant_context` **auto-commits on clean exit**: it uses
  `async with factory() as session: async with session.begin():`,
  which commits on clean `__aexit__` and rolls back on exception
  (`core/repositories/_session.py`). Splitting work across multiple
  contexts therefore persists each correctly.
- The tenant/user GUCs are **transaction-scoped** (`SET LOCAL`
  semantics via `set_config(name, value, true)`). Every new
  `tenant_context` re-applies them on entry, so splitting one long
  transaction into several short ones keeps every statement correctly
  scoped — RLS and the audit trigger continue to see `app.tenant_id`
  and `app.user_id`. This is the property that makes the refactor safe.
- `IDLE_TIMEOUT = 8h`, `ABSOLUTE_TIMEOUT = 24h`
  (`services/auth/session.py`). A 60-second touch-throttle is invisible
  to the idle-timeout's purpose.
- `SessionDTO` carries `last_seen_at` (`services/auth/session.py`).
- Engines are created with SQLAlchemy `QueuePool` defaults
  (`pool_size=5`, `max_overflow=10`) in `web/main.py`.

This decision is security-, audit-, and availability-relevant. It bears
on BAIT AT 7.2 / VAIT Chapter 7 (availability and operational
resilience of the authorisation surface), the ADR-0036 idle-timeout
control (which must be preserved), and ADR-0035 §4 (every statement
must run inside a correctly-scoped tenant context).

---

## Decision

### 1. The authentication dependency stops holding a request-scoped transaction

`get_authenticated_session` is **retired as a yield-dependency**. The
session that authenticated routes receive will no longer be a
long-lived, request-scoped `tenant_context`. Two consequences follow.

**1a. The touch runs in its own short, immediately-committed
transaction, throttled.** A new `require_authenticated_session`
dependency (non-yielding) resolves the `SessionDTO`, runs a throttled
`touch` in a self-contained `tenant_context` that opens, updates, and
commits in milliseconds, then returns the DTO. The lock is taken and
released within a single statement instead of being held for the
request.

The throttle is a single atomic conditional UPDATE — no read-then-write
race:

```sql
UPDATE sessions SET last_seen_at = NOW()
WHERE id = :sid
  AND last_seen_at < NOW() - make_interval(secs => :window)
```

with `TOUCH_THROTTLE_SECONDS = 60`. On the common path (a session
touched within the window) the statement matches zero rows, dirties
nothing, and holds no lock beyond the statement. This collapses the
per-request write storm to at most one write per 60 s per session.

**1b. `get_authenticated_user` opens its own short context for the user
load.** The single genuine consumer of the old yielded session is
`get_authenticated_user`, which runs `UserRepository.get_by_id`. It is
changed to open its own brief `tenant_context` for that read and close
it immediately — consistent with the dominant pattern already used by
every Pattern B route. It no longer inherits a request-scoped session.

### 2. Handlers own their transaction scope; dependencies provide context, not open sessions

The sanctioned pattern, codified here, is:

> **Dependencies resolve and return context (engine, tenant id, user
> id, role) as plain values/DTOs. Handlers open a `tenant_context` per
> discrete unit of database work and let it commit and close promptly.
> No transaction — and no pool connection — is held open across non-DB
> work (Excel parsing, dry-run extraction, SAA optimisation, external
> HTTP/LLM calls, template rendering).**

This is already how ~90% of the route handlers are written. The change
removes the one place (`get_authenticated_session`) that violated it,
and brings the dependency layer into line with the handler layer rather
than the reverse. It is therefore additive-in-spirit (a new dependency
alongside a retired one) rather than a sweeping rewrite of handlers.

### 3. Redundant in-handler touches are removed where the dependency already covers them

Once `require_authenticated_session` performs the throttled touch in
the dependency layer, the route handlers that **also** call `touch()`
inside their own work transaction are doing it twice. Specifically:

- `_render_preview_body` (`web/routes/data_import.py`) — its internal
  `touch()` is the T2 that self-blocks. It is **removed**; the
  dependency-layer touch on the gated route already reset the idle
  timer for the request. This is the change that eliminates the
  self-block at its source, independent of 1a.
- `load_data_import_section_context` (`web/routes/data_import.py`) — same
  removal.

For the Pattern B GET routes (charts, statistics, areas,
portfolio_review, limits, benchmarks_attribution), the in-handler
touch is **converted to `touch_throttled`** rather than removed,
because those routes depend on `require_session` (DTO only) and do
**not** pass through the touch-performing dependency. Throttling keeps
the idle-timer reset on those paths while ensuring that within the
throttle window the statement matches zero rows and acquires no lock —
removing the latent contention without changing the routes' dependency
wiring. (Routes that wish to migrate to `require_authenticated_session`
later can drop their internal touch entirely; that is a follow-up, not
part of this ADR.)

### 4. Defensive pool hardening

The app engine and audit engine are created with `pool_pre_ping=True`
(transparently recovers connections severed by a Postgres restart — the
earlier "Connection refused" / poisoned-pool situation) and the app
engine additionally with `pool_timeout=10` (a future pool-exhaustion
bug surfaces as a loud, fast `TimeoutError` instead of an indefinite
hang). `pool_size` / `max_overflow` are left at defaults; sizing is a
separate, measurement-driven decision once concurrency is observed on
the Hetzner deployment.

---

## Consequences

### Positive

- The deterministic upload hang is eliminated at its structural root:
  no request-scoped transaction is held across the dry-run extraction,
  and the self-blocking second touch is gone.
- No route holds an `idle in transaction` connection across non-DB
  work. Pool slots are occupied only for the duration of actual DB
  units of work, materially improving concurrency headroom before any
  pool-size tuning.
- The write load on the `sessions` table drops from one UPDATE per
  authenticated request to at most one per 60 s per session.
- The dependency and handler layers now follow one consistent
  transaction-scope discipline, which is simpler to reason about and to
  review.
- RLS scoping and audit-actor binding are preserved unchanged: every
  `tenant_context` re-applies the `SET LOCAL` GUCs on entry, so every
  statement — touch, user load, and handler work — runs correctly
  scoped.

### Negative / trade-offs

- This is a refactor of the authentication hot path, which every
  authenticated request flows through. Correctness is critical and the
  change is protected by a dedicated regression test (see below) and
  the existing auth/web suites.
- `get_authenticated_user` now performs its user load in a separate
  short transaction rather than reusing one already-open session. This
  is one extra connection acquire/release per gated request. Given the
  pool and the sub-millisecond nature of a `users.id` primary-key
  lookup, the cost is negligible and is paid back many times over by
  not holding a connection across the whole request.
- Pattern B routes keep their own work-transaction touch (now
  throttled). They are not migrated to the new dependency in this ADR;
  that consistency cleanup is deferred to avoid widening the change.

### Neutral / deferred

- Pool sizing remains at SQLAlchemy defaults; tuning is deferred to
  load testing (roadmap entry).
- Full migration of Pattern B routes onto `require_authenticated_session`
  (so they too can drop their internal touch) is a future tidy-up, not
  required for correctness after this ADR.

### Regression protection

A regression test asserts the invariant directly: a deliberately slow
authenticated handler must not hold an `idle in transaction` connection
across its slow section, and two concurrent requests on the same
session must not serialise on the `sessions` row. The test queries
`pg_stat_activity` from a second connection while the slow handler runs.
See the companion regression-test design.

---

## References

- ADR-0035 — Multi-tenant architecture, tenant isolation via RLS
  (`tenant_context`, `SET LOCAL` GUCs).
- ADR-0036 — Authentication strategy; idle/absolute timeouts; the
  `touch` idle-timeout reset this ADR throttles but preserves.
- ADR-0063 — Multi-tenant activation; the `require_role` chain whose
  dependency held T1 open.
- `core/repositories/_session.py` — `tenant_context` (auto-commit via
  `session.begin()`; `SET LOCAL` GUCs).
- `web/auth.py` — `get_authenticated_session` (retired yield-dependency),
  `get_optional_session`, `require_session`.
- `web/permissions.py` — `get_authenticated_user`, `require_role`.
- `services/auth/session.py` — `SessionRepository.touch`,
  `IDLE_TIMEOUT`, `ABSOLUTE_TIMEOUT`, `SessionDTO`.
- `web/routes/data_import.py` — the self-blocking upload route and
  `_render_preview_body`.
