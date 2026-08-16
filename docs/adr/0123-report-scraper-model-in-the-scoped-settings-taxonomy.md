# ADR-0123: Report Scraper Model in the Scoped-Settings Taxonomy — Per-Tenant Resolution for the One-Shot Extraction Path

- **Status:** Accepted (2026-08-15)
- **Date:** 2026-08-15
- **Deciders:** PortfoliFLOW project owner
- **Closes:** the last unconverted **web** consumer of the process-global
  `AIServiceCore` singleton left open by ADR-0112 §4b / strand F4 (the Report
  Scraper). Release-gating bug fix.
- **Supersedes / amends:** **annex amendment to ADR-0112 §3** (one new
  `openrouter` config field). ADR-0112 itself remains immutable and otherwise
  unchanged. Amends **ADR-0053 §"model dropdown"**: the Scraper page no longer
  offers a model selector; the model is a tenant setting like every other LLM
  choice. ADR-0053 remains authoritative for the upload / keyword-editor / SSE
  shape of the surface. ADR-0027 (Scraper implementation, capability map,
  message builder) is unchanged.
- **Tags:** scraper, configuration, multi-tenancy, credentials, admin, openrouter

---

## Context

The Report Scraper (`services/scraper/`, web surface `web/routes/scraper.py`,
ADR-0027 / ADR-0053) is currently broken on every deployment that does not
carry `OPENROUTER_API_KEY` in `.env`, and architecturally inconsistent on
every deployment that does:

1. **Wrong seam.** `ScraperService._scrape_one` calls
   `get_ai_service_core().send_one_shot_extraction(messages, model)`. That
   method still gates on the singleton triple plus `ConnectionStatus.CONNECTED`
   and constructs its client from the singleton's parked credentials. Since
   ADR-0112 §4b the web chat, the Irene beat and the Telegram bot resolve their
   endpoint, credential and model **per turn, per tenant** through
   `CredentialResolver` and hand a `ResolvedLLM` into the core; the singleton is
   parked by `web/main.py::_configure_ai_core` *only* for the two one-shot
   consumers F4 did not convert (Report Scraper, Fetcher-LLM). A tenant whose key
   lives in the vault — the multi-tenant default — therefore sees
   `Extraction failed: API call failed: RuntimeError: AIServiceCore not
   connected. Call configure() first.` on every file, while Shirley on the same
   tenant works.

2. **Wrong place for the model choice.** The Scraper page renders a model
   `<select>` sourced from `_MODEL_ALLOWLIST` (seven hard-coded ids, OpenRouter
   snapshot 2026-05-15) intersected with the capability map
   (`anthropic/claude-*`) — three options today, all failing for reason 1.
   Every other LLM consumer takes its model from Admin → Providers &
   Credentials (`openrouter.model` for Shirley, `openrouter.irene_model` for the
   Watch Desk); the Scraper is the only surface with a per-run model picker,
   and the only one whose model list is a static allowlist rather than the
   live catalog offered by the Admin card.

3. **Cost and isolation.** With the parked singleton, every tenant's
   extraction runs on the operator's application-scope key — exactly the
   posture ADR-0112 moved OpenRouter out of.

Verified preconditions (2026-08-15 snapshot): `ResolvedLLM` and its
`make_client()`; `AIServiceCore._make_async_client(llm=)` and the
`run_synthesis(llm=|model=)` mutual-exclusion contract; `resolve_config`'s
per-field chaining with the `scopes=` filter; the Irene chain in
`services/scheduler/tick_runner.py::_resolve_irene_llm`; the taxonomy-driven
Admin cards with `_MODEL_FIELD_KEYS` / `_FIELD_LABELS` / `_FIELD_HINTS` and the
`has_model_list` datalist affordance in
`web/templates/_partials/provider_credentials_section.html`; TX-04/TX-06 pins
in `tests/services/credential_vault/test_taxonomy.py`.

## Decision

### 1. One new config field: `openrouter.scraper_model` (tenant scope)

The `openrouter` declaration gains one non-secret field:

| Field | Kind | Scopes | Env link |
|---|---|---|---|
| `scraper_model` | config | tenant | `SCRAPER_MODEL` (`_ENV_CONFIG_FIELDS`) |

Tenant-only, mirroring `irene_model`: the Scraper is a tenant tool, not a
personal one, and a user-scope model would only widen the surface on which a
non-PDF-capable model can be chosen. Public label: **"Report Scraper model"**.
The declaration order becomes `api_key · model · scraper_model · irene_model ·
base_url` so the three model rows render as one block on the Admin card
(Shirley model → Report Scraper model → Watch Desk model → Base URL). Field
order in the taxonomy is presentational only; nothing chains on it.

No new provider, no new secret, no schema change: `scoped_settings` rows are
`(scope, provider, key)`; the migration-free extensibility this is exactly what
ADR-0112 §3 built the taxonomy for.

### 2. Resolution chain — scope-major, Scraper-first within each scope

Per run, inside the requesting session's `tenant_context`, the Scraper resolves:

- **credential** — the `openrouter` credential through `resolver.resolve()`
  (vault user → vault tenant → env `OPENROUTER_API_KEY`), unchanged façade;
- **model** — `tenant scraper_model → tenant model → env SCRAPER_MODEL → env
  SHIRLEY_MODEL → _DEFAULT_SCRAPER_MODEL`, the exact shape of the Irene chain
  so an operator who has configured nothing keeps the pre-F4 behaviour and a
  tenant that sets `scraper_model` overrides both environment variables;
- **base_url** — `tenant base_url → env OPENROUTER_BASE_URL →
  WebSettings.openrouter_base_url`, as everywhere else.

`_DEFAULT_SCRAPER_MODEL = "anthropic/claude-sonnet-4.5"` (the same built-in
default the Irene tick carries). *Considered:* no built-in default, failing
loudly like Shirley's `_NO_LLM_MESSAGE`. Rejected for symmetry with Irene and
because the capability gate below already turns any unsuitable resolution into
a loud, actionable error — the default is never silent about *whether* it can
extract.

**The capability gate stays.** `lookup_capability(resolved_model)` still runs
before any file is touched (ADR-0027, no silent fallback). A resolved model
outside the capability map is a hard, operator-readable refusal that names the
model and points at Admin → Providers & Credentials → OpenRouter → Report
Scraper model — not at "the Assistants settings", which no longer exist. The
capability map itself (`anthropic/claude-*` via `openrouter_file`) is not
touched by this ADR.

Resolution is **never stashed** (ADR-0112 §4b, chat's D3): `POST /scraper/runs`
resolves to fail fast (400 with the operator message when no credential or an
unsupported model), the SSE `GET .../stream` resolves again and that second
resolution drives the run. The `_PendingRun` keeps the model *id* for its log
line — an id is not a secret — but never the `ResolvedLLM`.

### 3. `send_one_shot_extraction` gains the `llm=` seam

`AIServiceCore.send_one_shot_extraction(messages, model=None, …, *, llm=None)`
takes the same `llm | model` contract as `run_synthesis`: exactly one of the
two; with `llm` the client comes from `llm.make_client()` and the model from
`llm.model`, and the singleton triple / `CONNECTED` status are **not
consulted**; with `model` alone the singleton path behaves verbatim as today
(the Fetcher-LLM keeps working unchanged). `ScraperService.scrape_reports`
takes `llm: ResolvedLLM` in place of `model: str` and stays DB-free, FastAPI-free
and Qt-free — `ResolvedLLM` is a plain value object from a module the service
already imports.

### 4. Web surface: read-only model line, no picker

`GET /scraper/section` shows the model the next run will use — resolved
through the config chain only, no credential needed — with a hint pointing at
the Admin field. When the resolved model fails the capability gate, the section
renders a notice instead of the form. `_MODEL_ALLOWLIST`,
`_scraper_model_options`, the `model` form field, the `<select>` and the
"three Anthropic models" hint are removed. Once this lands, `web/main.py`'s
parked singleton has exactly one remaining consumer — the Fetcher-LLM
(`services/web_research`) — and its docstring / startup log say so.

### 5. Admin surface

`_MODEL_FIELD_KEYS` includes `scraper_model` (the "Load models" catalog button
serves it, tenant scope only, owner-gated like the other two); `_FIELD_LABELS`,
`_FIELD_HINTS`, `_PROVIDER_DESCRIPTIONS["openrouter"]` and the `api_key` hint
name the Report Scraper; the template's `has_model_list` set includes the key.
`.env.example` documents `SCRAPER_MODEL` next to `IRENE_MODEL`.

## Consequences

- Any tenant with an OpenRouter credential in *any* scope can run the Scraper;
  a tenant row applies on the next run, no restart. Cost attribution follows
  the tenant's key.
- The Scraper's model is chosen where every other model is chosen; the Scraper
  page loses its only tenant-configuration widget.
- `web/main.py` still parks application-scope credentials — for the
  Fetcher-LLM only. Converting that consumer is a separate decision (it runs in
  tool threads with a `ToolExecutionContext`, not a session) and is **not** in
  scope here.
- Tests to update: TX-04/TX-06 pins (`test_taxonomy.py`), the resolver config
  suite, `test_scraper_service.py` (service takes `llm`), `test_scraper_section.py`
  (no dropdown; env-driven resolution like `test_chat_llm_resolution.py`),
  `test_provider_credentials.py` (third model field, datalist, gate),
  `test_ai_service_wiring.py` if it asserts log text.

## Not in scope

Fetcher-LLM conversion; capability-map extension to non-Anthropic providers;
user-scope `scraper_model`; result persistence for Scraper runs; the future Qt
Scraper widget.
