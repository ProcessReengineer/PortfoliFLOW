# ADR-0047: Tool-Execution Context Propagation — Tenant + Engine Seam for Postgres-Native AI Tools

- **Status:** Accepted
- **Date:** 2026-05-14
- **Deciders:** PortfoliFLOW project owner
- **Tags:** web-migration, ai-service, multi-tenant, persistence, architecture, strangler

---

## Context

Shirley's four existing AI-callable data tools (`list_datasets`,
`get_dataset_summary`, `get_dataset_slice`, `list_analysis_results` in
`services/tools/datastore_tools.py`) read from the process-global
in-memory `DataStore` singleton via `get_data_store()`. The PyQt6 GUI
populates that singleton on Excel import; the web variant does not —
per ADR-0041 the web Excel-import path writes to Postgres through the
repository layer (`investments`, `investment_navs`,
`investment_cashflows`). In a web chat session Shirley's data tools
therefore query an empty store.

The fix is a new family of Postgres-native tools that read through
`InvestmentService` and `tenant_context()`, exactly like every FastAPI
route. The one genuine obstacle: the synchronous tool-execution loop in
`AIServiceCore._stream_response_locked` dispatches tools via
`ToolRegistry.execute_tool(name, arguments)`, which carries only what
the LLM put in the tool call. There is **no channel** carrying the
request's tenant identity or the DB engine down to the tool function.
The `ToolRegistry` is a process-global singleton with import-time
registration; tool functions are plain `Callable[..., str]` with no
access to the FastAPI `request`.

A second obstacle is async-from-sync: `InvestmentService` and
`tenant_context()` are async, but the tool functions must be
synchronous (the `ToolRegistry` contract) and run inside the live
event loop already driving `stream_response`. Calling `asyncio.run()`
there raises `RuntimeError: asyncio.run() cannot be called from a
running event loop` — the bug ADR-0038 already fixed for
`send_one_shot_extraction`.

This decision is **audit-relevant**: it touches tenant isolation
(ADR-0035) — the channel built here carries the tenant identity that
RLS evaluates against, and the security boundary of who is allowed to
set it.

## Decision

PortfoliFLOW adds a **module-level tool-execution context** in
`services/tools/_tool_context.py`: a frozen `ToolExecutionContext`
dataclass carrying the current turn's `tenant_id: UUID` and
`engine: AsyncEngine`, with `set_tool_context` / `get_tool_context` /
`clear_tool_context` accessors over module-level state.

The chat route (`web/routes/chat.py::chat_stream`) populates the
context immediately before driving `core.stream_response(...)` and
clears it in a `finally` afterwards, so a turn that errors, an
upstream error event, or a client disconnect cannot leak context into
the next turn. The Postgres-native tools
(`services/tools/investment_tools.py`) call `get_tool_context()`; a
`None` return is the graceful-degradation signal for the GUI path
(which imports the tool module but never populates the context) — the
tools return a clear explanatory string rather than raising.

For the current single-tenant reality, `resolve_tenant_id()` returns
`core.tenant_constants.SENTINEL_TENANT_ID`. This function is the
**single hardwire seam** the forthcoming multi-tenant conversion
rewires — the dataclass, the accessors, the tool code, and the
chat-route wiring are all multi-tenant-agnostic by construction.

Async repository workflows are bridged from the synchronous tools via
`run_async_in_fresh_loop` in `services/tools/_async_bridge.py`: the
fresh-loop-on-a-daemon-thread pattern ADR-0038 established for
`send_one_shot_extraction`, extracted into one shared helper.

### Amendment (2026-05-14): the context carries the database URL, not the engine

The original decision had the context carry `engine: AsyncEngine`. The
Phase-4 smoke test surfaced a `RuntimeError: ... got Future ...
attached to a different loop`: the tools run their workflows on a
fresh event loop on a daemon thread (`run_async_in_fresh_loop`), but
the shared `AsyncEngine` was created on the uvicorn loop in
`web/main.py`'s lifespan. A SQLAlchemy `AsyncEngine` backed by asyncpg
holds a connection pool, and every asyncpg connection is bound to the
event loop it was created on — pulling one from the shared pool on the
fresh loop is the exact bug.

The amended decision: the context carries `database_url: str` — a
plain, immutable string — instead of the engine. Each tool workflow
constructs its own short-lived, loop-local `AsyncEngine` from that URL
*inside* the fresh loop (via the `_tool_session` async context manager
in `investment_tools.py`) and disposes it when the workflow ends. An
immutable string crosses a thread/loop boundary safely; a live engine
does not.

The chat route now populates the context from
`request.app.state.settings.database_url` — request-scoped plumbing
from settings — and the guard flips from "engine is not `None`" to
"database URL is truthy". `resolve_tenant_id()` is unchanged: it
remains the *single* hardwire seam; the URL is ordinary settings
plumbing, not a second seam.

## Rationale

- **ADR-0041 compliance.** The tools read through the repository layer
  exactly as ADR-0041 §1 prescribes for the web surface.
  `get_data_store()` is untouched and still means "in-memory,
  GUI-flavoured, nothing else." No stale-cache hydration bridge is
  introduced.
- **Multi-tenant-ready in shape.** The context already carries a
  `tenant_id`; only its *resolution* is hardwired. The successor run
  changes one function body, not a data model.
- **Minimal blast radius.** No change to the `ToolRegistry` signature,
  the `stream_response` signature, or the Qt adapter. The GUI path is
  unaffected — the new module degrades gracefully there.
- **Module-level state is safe here, today.**
  `AIServiceCore._stream_response_locked` runs under the process-wide
  `_TURN_LOCK` (ADR-0031), so at most one turn is ever populating the
  context at a time in a single-worker deployment — there is never
  concurrent population to race on.
- **Auditability over convenience.** The single-tenant assumption is
  concentrated in one named, commented function (`resolve_tenant_id`)
  rather than scattered through the tool code — a reviewer can see
  exactly where it lives and what replaces it.

## Alternatives Considered

- **Hydration bridge into `get_data_store()`** — load the Postgres
  universe into the in-memory singleton on chat-page load, so the
  existing `datastore_tools.py` tools "just work" on the web. Rejected:
  it violates ADR-0041 §1 (it makes `get_data_store()` mean Postgres
  data, context-dependent — exactly the "config-flag switch" ADR-0041
  rejected), and it bakes in the tenant-blind global singleton as a
  cross-tenant data leak waiting to happen once multi-user lands.
- **Per-request `ToolRegistry` instance** — give each request its own
  registry carrying the context. Rejected for this phase: too large a
  change to the registry and the `stream_response` signature, and it
  touches the Qt path. Revisit if multi-tenant work makes a
  per-request registry the natural shape.
- **Tenant id as an LLM-supplied tool argument** — let the model pass
  the tenant id in the tool call. Rejected outright: the tenant
  boundary is a security boundary the model must never control.
- **Do nothing / status quo** — leave Shirley's data tools querying an
  empty store on the web. Rejected: the web chat surface would have no
  working investment data tools at all.

## Consequences

### Positive

- Shirley's web chat surface gains three working, RLS-scoped,
  read-only investment tools (`list_investments`,
  `get_investment_detail`, `get_investment_nav_history`).
- The async-from-sync bridge is now one shared, tested helper
  (`run_async_in_fresh_loop`) instead of three copy-pasted thread
  dances.
- The single-tenant assumption is isolated to one function body; the
  multi-tenant run has a clearly-marked, minimal seam to rewire.

### Negative

- A new piece of module-level mutable state exists in `services/`. It
  is justified by the `_TURN_LOCK` invariant, but it is state, and a
  future concurrency change must account for it (see Follow-ups).
- `resolve_tenant_id()` is a deliberate, time-boxed simplification —
  known, accepted technical debt until the successor run.

### Neutral / Follow-ups

- **Multi-worker / concurrent turns.** A multi-worker deployment (the
  same one that needs Redis for `pending_turns`, flagged in
  `web/routes/chat.py`), or any future concurrent-turn design,
  requires this context to become `contextvars`-based. That is the
  known migration trigger — noted in the `_tool_context.py` docstring.
- **Multi-tenant conversion (next run).** Replaces the body of
  `resolve_tenant_id()` with real per-request tenant resolution from
  the authenticated `session.tenant_id`. The context object, the tool
  functions, and the chat-route wiring stay.
- **`send_one_shot_extraction` tidy-up.** That method could later be
  refactored to call `run_async_in_fresh_loop` too. Deliberately *not*
  done here — it would touch a just-stabilised method.
- **`datastore_tools.py` retirement.** When the GUI itself migrates to
  Postgres (ADR-0041 §2 Phase-4 work), the in-memory tool family can
  be retired. Until then both families coexist in the registry.

### Amendment consequences (2026-05-14)

- **Cross-loop constraint, made explicit.** A SQLAlchemy `AsyncEngine`
  / asyncpg connection pool is bound to the event loop it was created
  on. The tools run on a fresh thread loop, so a shared engine cannot
  be handed in — the context carries the URL and each tool builds its
  own loop-local engine. This is the same hazard class as the
  `httpx.AsyncClient` loop-binding note in `AIServiceCore.__init__`,
  and the same per-job-engine pattern as
  `web/main.py::_read_schema_revision`.
- **Cost: a fresh engine per tool call.** Each call opens a new pool,
  runs the asyncpg connection handshake, and tears it down. At
  Shirley's human-paced call volume this is negligible, and it matches
  the tradeoff `_read_schema_revision` already accepts. A per-thread /
  per-loop engine cache keyed by loop identity is a possible future
  optimisation — explicitly **not** built now.

## Implementation Notes

- Affected modules / files:
  - `services/tools/_tool_context.py` (new) — `ToolExecutionContext`,
    `set`/`get`/`clear`, `resolve_tenant_id` (the hardwire seam).
  - `services/tools/_async_bridge.py` (new) — `run_async_in_fresh_loop`.
  - `services/tools/investment_tools.py` (new) — the three
    Postgres-native `READ_INTERNAL` tools.
  - `services/ai_service_core.py` — one import line in
    `_register_default_tools` (the core now registers four default
    tool modules, not three).
  - `web/routes/chat.py` — context set/clear bracket in
    `chat_stream`'s `event_stream`.
- Related tests:
  - `tests/assistants/test_tool_context.py` — the seam and the
    async bridge.
  - `tests/assistants/test_investment_tools.py` — context-not-set
    degradation (no DB) + DB-backed happy path.
  - `tests/regression/test_ai_service_core_qt_free.py` — guards that
    `_tool_context.py` does not pull PyQt6 into the `services/` import
    graph.
- Layering: `_tool_context.py` imports only `sqlalchemy`, the stdlib,
  and `core.tenant_constants`; it must not import from `web/` (the
  chat route imports *from* it) and must not import PyQt6 (ADR-0038).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Security (tenant
  isolation channel), Maintainability (single, named multi-tenant
  seam), Reliability (context is cleared in a `finally`, so a failed
  turn cannot leak tenant context forward).
- **Regulatory references:** Supports the tenant-isolation posture of
  ADR-0035 (RLS) — the tenant id RLS evaluates against now has a
  defined, auditable propagation path into the tool layer.
- **Audit evidence:** `resolve_tenant_id()` in
  `services/tools/_tool_context.py` (the single hardwire point, with a
  `# TODO(multi-tenant)` marker); the context set/clear bracket in
  `web/routes/chat.py::chat_stream`; the test suite listed above.

## References

- Related ADRs: ADR-0041 (persistence entry-points — this decision
  does not reintroduce its rejected hydration bridge), ADR-0038
  (Qt-free core + the fresh-loop async-from-sync pattern), ADR-0035
  (multi-tenant isolation via RLS), ADR-0031 (module-level threading
  lock — the invariant that makes module-level context state safe),
  ADR-0022 (tool trust classes — the new tools are `READ_INTERNAL`).
- Predecessor work: `shirley-smoke-test-fixes` (loop-safe
  `send_one_shot_extraction`, native-EventSource chat surface).

---

## Revision History

| Date       | Author                  | Change        |
|------------|-------------------------|---------------|
| 2026-05-14 | PortfoliFLOW project owner | Initial draft, status Accepted |
| 2026-05-14 | PortfoliFLOW project owner | Amended — the context carries the database URL string, not the `AsyncEngine`; each tool builds its own loop-local engine. Followed from the cross-loop `RuntimeError` found in the Phase-4 smoke test. |
