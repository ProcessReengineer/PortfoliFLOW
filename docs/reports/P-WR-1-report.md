# P-WR-1 report — web research LLM per tenant (ADR-0132)

Run: 2026-09-28. Prompt version v3. Baseline tip `1838e14`, tree clean at start.
Verify-first passed on all 18 rows — no STOP. v3's three re-pinned numbers were
all confirmed exactly (check 8 = 10 / 7; check 17 = 14/5/15/26/23/3; check 18 =
56 collected from 41 functions).

## Operator action required

1. **Restart `portfoliflow-web`.** The taxonomy gained a field; the Admin card
   reads the taxonomy live, but the running process predates the code.
2. **Admin → Providers & Credentials → OpenRouter: set "Web research model"**
   for the tenant — *optional*. Left empty, the Shirley model serves, exactly as
   before. No `.env` change is required: `OPENROUTER_API_KEY` and
   `SHIRLEY_MODEL` stay valid as the **application scope** of the chain.
   The 2026-09-25 failure mode is structurally gone — a stale `.env` key no
   longer affects a tenant that holds a vault row, because the vault tenant
   scope now outranks it on this path as it already did for chat.
   (Your `.env` currently carries both `OPENROUTER_API_KEY` and `SHIRLEY_MODEL`
   and no `RESEARCH_MODEL`; that resolves fine — the news path will use the
   tenant's vault key if one exists, else the `.env` key, and the Shirley model.)
3. **`tests/web` ran against the dev database.** Run `portfoliflow bootstrap`
   before using the app locally — the suite truncates `users` / `tenants`.
   The dev Postgres container (`portfoliflow-postgres`) was stopped at session
   start and was started to run the DB-backed gates; it is still running.
4. **Commit** with the message in §Commit. Run the full suite at home.

## Verify-first

| # | Check | Expected | Actual | Verdict |
|---|---|---|---|---|
| 1 | tree clean; tip | clean at `1838e14` | clean; `1838e14` | ✅ |
| 2 | `get_model()` in `web_research/service.py` | 2 | 2 | ✅ |
| 3 | `send_one_shot_extraction(` there | 2 | 2 | ✅ |
| 4 | `llm: ResolvedLLM \| None = None` in `ai_service_core.py` | one hit in `send_one_shot_extraction`'s signature | present at L962 inside `send_one_shot_extraction` (L955); 6 hits file-wide across sibling signatures — the row is a `-n` listing, not a count | ✅ (see Deviations 1) |
| 5 | `_configure_ai_core` in `web/main.py` | 3 | 3 | ✅ |
| 6 | `ToolExecutionContext(` in chat / bot | 1 each | 1 / 1 | ✅ |
| 7 | `user_id` in `_tool_context.py` | no hits | no hits | ✅ |
| 8 | `_tool_session` counts | 10 / 7; `-rl` names exactly those two files | 10 / 7; exactly `investment_tools.py`, `analysis_tools.py` | ✅ |
| 9 | `"scraper_model": "SCRAPER_MODEL"` | one hit, 4 entries | L148, 4 entries | ✅ |
| 10 | `ProviderField(name="scraper_model"` | one hit, then `irene_model`, `base_url` | L205 → L206 `irene_model` → L207 `base_url` | ✅ |
| 11 | `_MODEL_FIELD_KEYS` | `{"model","scraper_model","irene_model"}` | L153, exactly that | ✅ |
| 12 | template tuple | 1 | 1 | ✅ |
| 13 | next free ADR is **0132** | 1 | 1 — ADR-0132 used, README bumped to 0133 | ✅ |
| 14 | Next free ID `#069` | 1 (note only) | 1 — unchanged, no item claimed | ✅ note |
| 15 | `application scope alone` in architecture.md | 1 (note only) | 1 — corrected in §6.4 | ✅ note |
| 16 | report file absent | absent | absent | ✅ |
| 17 | six-module collection | 14 / 5 / 15 / 26 / 23 / 3 | 14 / 5 / 15 / 26 / 23 / 3 (86 total) | ✅ |
| 18 | `test_provider_credentials.py` collection | 56 | 56 (41 functions) | ✅ |

## Files

| Path | State | What |
|---|---|---|
| `services/tools/_tool_session.py` | **new** | `tool_session(ctx)` lifted from `investment_tools.py`; opens the loop-local engine and enters `tenant_context(..., user_id=ctx.user_id)`; docstring states the GUC-not-RLS point |
| `services/web_research/llm.py` | **new** | `_DEFAULT_RESEARCH_MODEL`, `_DEFAULT_OPENROUTER_BASE_URL`, `NO_RESEARCH_LLM_MESSAGE`, `ResearchLLMUnavailableError`, `resolve_research_model`, `resolve_research_llm` |
| `docs/adr/0132-…taxonomy.md` | **new** | the ADR (Nygard, six decision sections) |
| `tests/services/web_research/test_llm.py` | **new** | env-only chain, base-URL default, typed shortfall, `VaultDecryptError` not wrapped |
| `services/tools/investment_tools.py` | modified | local `_tool_session` deleted, 8 call sites renamed, 5 now-unused imports dropped, docstring repointed |
| `services/tools/analysis_tools.py` | modified | sibling import replaced, 5 call sites renamed, two authorised doc edits |
| `services/tools/_tool_context.py` | modified | `user_id: UUID \| None = None` + attribute docstring + module-docstring paragraph |
| `services/tools/web_research_tool.py` | modified | `_resolve_llm_for_call`, in-band shortfall envelope, INFO log line, `llm=` handed to the service; resolution paragraph in the docstring |
| `services/web_research/service.py` | modified | `research(..., *, llm)`, threaded through `_research_one` to both LLM methods; `get_model()` blocks removed; module + class docstrings corrected |
| `services/web_research/__init__.py` | modified | four new exports, package docstring sentence |
| `services/credential_vault/taxonomy.py` | modified | `research_model` field between `scraper_model` and `irene_model`; two comment blocks and the module docstring |
| `services/investments/credential_resolver.py` | modified | `"research_model": "RESEARCH_MODEL"`; the `scopes=` docstring note |
| `services/ai_service_core.py` | modified | three docstrings that named the Fetcher-LLM as the singleton path's consumer (§9 permitted; no behaviour change) |
| `web/main.py` | modified | `_configure_ai_core` deleted; lifespan registers the instance only; 3 imports dropped |
| `web/routes/chat.py` | modified | `user_id=session.user_id` + comment |
| `bot/telegram_bot.py` | modified | `user_id=paired_user_id` + comment (already in scope at the site) |
| `web/routes/provider_credentials.py` | modified | `_MODEL_FIELD_KEYS`, label, hint, provider description, api-key hint, two comments, one docstring |
| `web/templates/_partials/…section.html` | modified | `has_model_list` tuple |
| `.env.example` | modified | `RESEARCH_MODEL=` block; the `OPENROUTER_API_KEY` block's "read at startup" claim corrected |
| `docs/adr/README.md` | modified | index row + update paragraph; next free **0132 → 0133** |
| `docs/architecture.md` | modified | §Configuration sentence — both halves (§6.4) |
| `docs/roadmap.md` | modified | one change-log row, 2026-09-28, "Next free ID unchanged" |
| 9 test modules | modified | see §Tests |

## Tests

| Module | Target | Actual | Note |
|---|---|---|---|
| `tests/services/web_research/test_service.py` | 14 → 16 | **16** ✅ | every `research()` call carries `llm=_FAKE_LLM` (a real `ResolvedLLM`); the two new tests pin the keyword-only requirement and `llm=` / no-`model=` on both stages |
| `tests/services/web_research/test_llm.py` (new) | +6 | **7** | +1 over target: the base-URL chain needed two rows (default, and env-overrides-default) |
| `tests/assistants/test_web_research_tool.py` | 5 → 8 | **9** | +1 over target: the prompt's case (b) carried three assertions; the ADR-0022 wrapping one is its own test, since it needs a registry rather than a bare call |
| `tests/assistants/test_tool_context.py` | 15 → 16 | **16** ✅ | |
| `tests/services/credential_vault/test_taxonomy.py` | 26 unchanged | **26** ✅ | TX-04 order pin now 5 fields; both TX-06 set pins gained the key |
| `tests/services/investments/test_credential_resolver_config.py` | 23 → 24 | **24** ✅ | |
| `tests/web/test_provider_credentials.py` | 41 → 43 fns, 56 → 60 collected | **43 / 60** ✅ | +2 functions, +2 parametrised rows, exactly as specified |
| `tests/web/test_ai_service_wiring.py` | 3 → 2 | **2** ✅ | the two lifespan tests replaced by `test_lifespan_leaves_the_singleton_untouched` |
| `tests/web/test_chat_llm_resolution.py` | +0/+1 | **10 → 11** | no test asserted the context's kwargs, so one was added (§5's "otherwise" branch); its `_FakeCore` now records `tool_context` |
| `tests/characterization/test_ai_service_core.py` | 0 | **19, unchanged** | comments reworded only |
| `tests/web/conftest.py` | — | — | `_ai_core_hygiene` docstring drops the `_configure_ai_core` reference; the teardown stays |

DB-backed modules **did not skip**: the dev Postgres container was stopped at
session start and was started, so every DB-gated test in the selection ran for
real — including the new `test_tenant_research_model_outranks_the_environment`,
which seeds a tenant-scope `research_model` row and proves it beats `.env`'s
`SHIRLEY_MODEL` through the live vault path.

## Gates

| Gate | Result |
|---|---|
| §7.1 `ruff check .` | `All checks passed!` |
| §7.1 `ruff format --check .` | `945 files already formatted` |
| §7.2 `pyright` (the four ADR-0110 islands) | `0 errors, 0 warnings, 0 informations` |
| §7.3 broad selection | **735 passed, 7 deselected**, 15 warnings, 787s — `tests/assistants`, `tests/services/web_research`, `tests/services/credential_vault`, `tests/services/investments`, `tests/web/test_provider_credentials.py`, `tests/web/test_ai_service_wiring.py`, `tests/web/test_scraper_section.py`, `tests/services/scheduler`. The Scraper and Irene chains are untouched and their counts unchanged. |
| §7.4 `get_model()` in web_research + tool | no hits |
| §7.4 `_tool_session(` in `services/` | no hits |
| §7.4 `investment_tools import _tool_session` in `services/` + `tests/` | no hits |
| §7.4 `_configure_ai_core` in `web/` + `tests/` + `architecture.md` | no hits (the ADR quotes the retired name once, in Context, as §7.4 allows) |
| §7.5 `no active model selected` in `services/` | no hits |
| §7.6 full suite | not run here, per the prompt — operator home run |

Independent confirmation of the §2 chain, walked directly against a stub
environment rather than through the tool: nothing set → `anthropic/claude-sonnet-4.5`;
`SHIRLEY_MODEL` alone → that; `RESEARCH_MODEL` + `SHIRLEY_MODEL` → `RESEARCH_MODEL`.

## Deviations

1. **Check 4 is a listing, not a count.** The row asks for "one hit inside
   `send_one_shot_extraction`'s signature"; the pattern matches six sibling
   signatures file-wide. Verified by mapping each hit to its enclosing `def`:
   the ADR-0123 seam is present at `services/ai_service_core.py:962`. Treated as
   a pass on substance, not a STOP — the check's stated expectation is the seam,
   and v3's own §10 lesson is that a count must be read in the unit the command
   measures.
2. **`_research_one` also takes `llm`.** §4.5 names `_pre_filter_feed_items` and
   `_call_fetcher_llm`; `_research_one` sits between `research` and the Fetcher
   call, so the resolution is threaded through it. Implied by the seam, not a
   choice.
3. **Two test counts are +1 over target** (`test_llm.py` 7 vs 6,
   `test_web_research_tool.py` 9 vs 8). Reasons in the §Tests table. No target
   was undershot.
4. **Three extra imports removed from `web/main.py`.** §4.8 authorises removing
   the unused `AIServiceCore` import; deleting `_configure_ai_core` also orphaned
   `ConnectionStatus` and `is_vault_configured`, which `ruff` (gate §7.1) would
   have failed on. Forced, not elective.
5. **`services/ai_service_core.py` docstrings touched.** §9 permits dropping the
   "keeps the Fetcher-LLM working" text; three places named the Fetcher-LLM as
   the singleton path's consumer, which the change makes false. Behaviour and
   signatures untouched; the characterization suite is unchanged and green.
6. **`_resolve_llm_for_call` takes the context as a parameter.** §4.6's sketch
   reads `get_tool_context()` inside the helper but uses `ctx` in the log line
   that follows, which requires it in the caller's scope. Read once in
   `web_research()` and passed down — the same seam, defined once.
7. **`tests/web/conftest.py` docstring edited.** Required by D-WR-8 ("its
   docstring drops the `_configure_ai_core` reference"), listed here because
   §5's table does not name the file.

## Open questions

None.

## Commit

```
feat(web-research): resolve the news LLM per tenant through the scoped-settings taxonomy, annex ADR-0132 (P-WR-1)
```
