# ADR-0132: Web Research Model in the Scoped-Settings Taxonomy — Per-Call, Per-Tenant Resolution for the News Path

- **Status:** Accepted (2026-09-28)
- **Date:** 2026-09-28
- **Deciders:** PortfoliFLOW project owner
- **Closes:** the conversion ADR-0123 §"Not in scope" left open — the
  Fetcher-LLM / Feed-Filter-LLM pair in `services/web_research/`, which was the
  **last** web consumer of the process-global `AIServiceCore` singleton that
  `web/main.py` parked from `.env`. Release-gating bug fix.
- **Supersedes / amends:** **annex amendment to ADR-0112 §3** (one new
  `openrouter` config field). ADR-0112 itself remains immutable and otherwise
  unchanged. **Amendment to ADR-0047**: `ToolExecutionContext` carries a third,
  optional field, `user_id` — the turn's authenticated person — so a tool that
  resolves credentials per call can consult the user scope. Amends **ADR-0123
  §Consequences**, whose bullet "`web/main.py` still parks application-scope
  credentials — for the Fetcher-LLM only" no longer holds: it parks nothing.
  ADR-0023 (two-stage research) and ADR-0024 (RSS resolution) are unchanged —
  neither the pipeline, the allowlist, nor the untrusted-content contract of
  ADR-0022 is touched.
- **Tags:** web-research, configuration, multi-tenancy, credentials, admin, openrouter, tools

---

## Context

The News Scraper (`services/web_research/`, exposed as the `web_research` tool)
runs two LLMs: the **Feed-Filter-LLM**, which picks the relevant candidates out
of the harvested RSS items, and the **Fetcher-LLM**, which extracts each
selected article into the validated schema ADR-0022 requires before any of it
reaches Shirley's conversation. Both read the process-global singleton. Two
failures followed, three days apart, on the same deployment:

On **2026-09-25** a news query failed with `WebResearchService: no active model
selected; cannot run Feed-Filter-LLM.` The singleton had no model because the
lifespan had nothing to park: `SHIRLEY_MODEL` was absent from `.env`, the
tenant's model living in the vault where every other consumer reads it from.
Shirley answered normally throughout the same session, because a chat turn
resolves its LLM per turn through `CredentialResolver` (ADR-0112 §4b) and never
consults the singleton at all.

After the operator re-parked application-scope credentials in `.env`, the same
query failed with `Feed-Filter-LLM call raised AuthenticationError: 401 — User
not found`. The `.env` key was stale while the tenant's vault key was current —
two keys for one provider, free to drift, with nothing in the system able to
report the drift, because no consumer reads both. This is precisely the posture
ADR-0112 moved OpenRouter out of, surviving in the one place F4 and ADR-0123
did not reach.

ADR-0123 named the exclusion and its reason: the news path "runs in tool threads
with a `ToolExecutionContext`, not a session". That is true and it is the whole
of the problem — the context carried a tenant and a database URL but no user, and
nothing in the tool layer opened a session to resolve against. The gap is two
small seams wide, not an architectural one.

Verified preconditions (2026-09-28 snapshot): the `llm=` parameter on
`AIServiceCore.send_one_shot_extraction` and its `llm | model` mutual-exclusion
contract (ADR-0123 §3); `resolve_config`'s per-field chaining with the `scopes=`
filter; the Scraper chain in `web/routes/scraper.py` and the Irene chain in
`services/scheduler/tick_runner.py::_resolve_tenant_llm`; the loop-local
`_tool_session` helper in `services/tools/investment_tools.py`, consumed by
`analysis_tools.py`; the taxonomy-driven Admin cards with `_MODEL_FIELD_KEYS` /
`_FIELD_LABELS` / `_FIELD_HINTS` and the `has_model_list` datalist affordance;
TX-04/TX-06 pins in `tests/services/credential_vault/test_taxonomy.py`.

## Decision

### 1. One new config field: `openrouter.research_model` (tenant scope)

The `openrouter` declaration gains one non-secret field:

| Field | Kind | Scopes | Env link |
|---|---|---|---|
| `research_model` | config | tenant | `RESEARCH_MODEL` (`_ENV_CONFIG_FIELDS`) |

Tenant-only, mirroring `scraper_model` and `irene_model`: web research is a
tenant capability, not a personal one. Public label: **"Web research model"**.
The declaration order becomes `api_key · model · scraper_model ·
research_model · irene_model · base_url` so the Admin card renders its four
model rows as one block (Shirley model → Report Scraper model → Web research
model → Watch Desk model → Base URL). Field order in the taxonomy is
presentational only; nothing chains on it.

No new provider, no new secret, no schema change: `scoped_settings` rows are
`(scope, provider, key)`, which is the migration-free extensibility ADR-0112 §3
built the taxonomy for.

### 2. Resolution chain — scope-major, research-first within each scope

Per **tool call**, inside the turn tenant's `tenant_context` with the user axis
carried:

- **credential** — `resolver.resolve("openrouter", tenant_id=…, user_id=…)`
  (vault user → vault tenant → env `OPENROUTER_API_KEY`), unchanged façade;
- **model** — `tenant research_model → tenant model → env RESEARCH_MODEL → env
  SHIRLEY_MODEL → _DEFAULT_RESEARCH_MODEL`, the exact shape of the Irene and
  Scraper chains with web research's field in their place, so an operator who
  has configured nothing keeps the pre-ADR-0132 behaviour;
- **base_url** — `tenant base_url → env OPENROUTER_BASE_URL →
  _DEFAULT_OPENROUTER_BASE_URL`.

`_DEFAULT_RESEARCH_MODEL = "anthropic/claude-sonnet-4.5"`, the same built-in
default Irene and the Scraper carry. The base-URL default is a **constant in
`services/web_research/llm.py`**, not `WebSettings.openrouter_base_url`:
`services/` must not import from `web/`, and the tool runs on a daemon thread
holding no request. The value is the same one the web settings carry.

**No capability gate.** Unlike the Report Scraper, whose extraction needs PDF
input and therefore an Anthropic model, both news LLMs are text-only: any
chat-capable model works, so there is nothing to refuse.

### 3. One model for both news LLMs

The Feed-Filter-LLM and the Fetcher-LLM share `research_model`. They are two
stages of one research call; two fields would double the Admin surface for a
decision nobody takes. *Considered:* a `filter_model` / `fetcher_model` split
so a cheap model could triage candidates and a stronger one extract. Rejected
for now, not foreclosed — the chain lives in **one** function
(`resolve_research_model`), so a later split is a local change.

### 4. The tool context gains the user axis

`ToolExecutionContext` gains `user_id: UUID | None = None`, after
`database_url`, defaulted so every existing constructor stays valid.
`web/routes/chat.py` passes `session.user_id`; `bot/telegram_bot.py` passes the
`paired_user_id` it already resolves for the turn (`None` when unpaired).

Without it a tool resolving credentials per call could consult only the tenant
scope, silently ignoring a user-scope row the same person's chat turn would
honour — an inconsistency between two surfaces of the same credential chain.
The field is read by nothing else, and it plays **no** part in RLS: tenant
isolation binds on `app.tenant_id` alone, exactly as before.

### 5. Resolution happens in the tool wrapper, per call, never stashed

`services/tools/web_research_tool.py` resolves before it runs the pipeline and
passes the result down. With a context it opens a loop-local, tenant-scoped
session inside `run_async_in_fresh_loop` and resolves through a vault-backed
`CredentialResolver`; **without** one (the desktop path, a DB-less contributor
laptop) it resolves through a session-less resolver — environment only — the
same graceful degradation `web/routes/scraper.py` takes.

The resolution is never stashed (ADR-0112 §4b, chat's D3): it lives in one
local, for one call, held on no module, context or service instance. A missing
credential becomes an **in-band envelope body** naming Admin → Providers &
Credentials → OpenRouter — `web_research` is a `READ_EXTERNAL_UNTRUSTED` tool
and never raises at the model. A `VaultDecryptError` propagates untouched: an
operator emergency must not read as "configure me".

The shared `_tool_session` helper is lifted out of `investment_tools.py` into
`services/tools/_tool_session.py` as `tool_session`, and both existing consumers
repoint at it — a third consumer made a tool module importing a private name
from a sibling tool module untenable. It cannot live in `_tool_context.py`,
which is stdlib-only by contract. The lift's one behavioural change is that
`user_id` now reaches `tenant_context`, setting the `app.user_id` GUC the b001
audit trigger reads (ADR-0036 §1d) — a no-op for all thirteen existing sites,
every one `READ_INTERNAL` and writing no audit rows, and correct for any future
`WRITE_INTERNAL` tool, whose audit rows will name the turn's user.

### 6. The service takes the resolution as an argument

`WebResearchService.research(query, max_articles=5, *, llm: ResolvedLLM)` —
keyword-only and required, so a caller that forgets it fails at the call rather
than falling back to a process-global model. `_pre_filter_feed_items` and
`_call_fetcher_llm` take `llm` and pass it to `send_one_shot_extraction`; the
`ai.get_model()` blocks and their "no active model selected" warnings are
removed. `harvest_items()` — Irene's RSS delta, which runs no LLM — is
untouched.

### 7. `web/main.py` stops parking credentials

With the last consumer converted, `_configure_ai_core` has no purpose and is
deleted. The lifespan sets `app.state.ai_core = get_ai_service_core()`: the
**instance** is still registered, because the chat route's `_ai_core` override
seam reads it, but it carries no credentials. `.env`'s `OPENROUTER_API_KEY` and
`SHIRLEY_MODEL` remain valid — as the **application scope** of the resolution
chains, consulted per call when no vault row serves, and nothing else.

`send_one_shot_extraction`'s singleton path is **unchanged**: the Qt-free
desktop entry point still uses it. It simply has no web consumer any more.

## Consequences

- Any tenant with an OpenRouter credential in *any* scope can run web research.
  A `research_model` row applies on the next query, with no restart, and cost
  follows that tenant's own key.
- A stale `.env` key can no longer break a tenant that holds a vault row — the
  2026-09-25 failure mode is structurally gone, because no consumer reads two
  keys any more.
- `web/main.py` parks nothing, and no consumer reads the application scope
  alone. `.env` is one link in a chain rather than a second source of truth.
- The tool context is one field wider, and one tool reads it. Its
  multi-worker/`contextvars` migration trigger (ADR-0047) is unchanged.
- Tests to update: TX-04/TX-06 pins (`test_taxonomy.py`), the resolver config
  suite, `test_service.py` (the service takes `llm`), `test_web_research_tool.py`
  (resolution, both shapes), `test_tool_context.py` (the user axis),
  `test_provider_credentials.py` (fourth model field, datalist, scope gate),
  `test_ai_service_wiring.py` (the lifespan parks nothing).

## Not in scope

An Admin UI for `config/web_research.yaml` (feeds, domains, windows) — that
file is audit-relevant by design and ADR-0023 / ADR-0024 stand; a user-scope
`research_model`; separate models for the two news LLMs (§3); the Qt desktop
path, which keeps the singleton one-shot seam.
