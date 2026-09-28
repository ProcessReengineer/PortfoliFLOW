# P-TG-H1 — Telegram bot observability: web logging, card status line, start-up copy

**Strand:** housekeeping, single commit. Ten files changed, one added.
**Prompt:** P-TG-H1, verified against the Repomix image of 2026-09-25 09:01 (version `2026.09.0`).
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`
**Run:** 2026-09-28. All twelve verify-first checks matched. Every §3 change applied and
every §4 gate is green: `ruff`, `pyright`, and all three test selections, the DB-backed
`test_provider_credentials.py` included (65 passed).
**Git:** no operations performed. Read-only git only (`status`, `diff`, `log`).
**Behaviour of the bot itself:** unchanged. Three observability fixes only.

---

## 1. OPERATOR ACTION REQUIRED

**1. `LOG_LEVEL`.** Nothing to do: `.env` already carries `LOG_LEVEL=INFO` (line 7),
and `.env.example` ships the same default. Set `LOG_LEVEL=DEBUG` only if you want the
debug lines too. An unknown value is now refused at settings load rather than silently
degraded, so `portfoliflow-web` will not start on a typo.

**2. Restart and confirm.** After the restart the start-up log carries the lines the
deploy doc tells you to look for — they existed before and went nowhere:

```
Telegram bot: starting; discovering tenant bot tokens.
Telegram bot [tenant=… source=vault]: dispatcher registered.
Telegram bot: polling N dispatcher(s).
```

Then open Admin → Providers & Credentials on any tenant: the Telegram card now carries
a status line above the pairing flow, which should read *The bot for this tenant is
running.*

**3. Commit.**

```sh
git add .env.example bot/config.py bot/telegram_bot.py \
        docs/deploy/telegram-multi-bot.md web/main.py web/settings.py \
        web/routes/provider_credentials.py \
        web/templates/_partials/provider_credentials_section.html \
        tests/bot/test_telegram_bot.py tests/web/test_provider_credentials.py \
        tests/web/test_web_run_logging.py docs/reports/P-TG-H1-report.md
git commit -m "fix(web,bot): configure logging under portfoliflow-web, show bot status on the Telegram card, calm the start-up copy (P-TG-H1)"
```

---

## 2. Verify-first values observed

Tip (check 1): `a3e45af fix(statistics): shade correlation cells with SVG shapes, not the
canvas raster (P-CORR-1)`. Working tree clean.

| # | Check | Expected | Observed | |
|---|---|---|---|---|
| 1 | `git status --porcelain`; tip | empty; any subject | empty; `a3e45af … (P-CORR-1)` | ✅ |
| 2 | `grep -c 'configure_logging' web/main.py` | `0` | `0` | ✅ |
| 3 | `grep -c 'uvicorn.run(' web/main.py` | `1` | `1` | ✅ |
| 4 | `def configure_logging` in `core/logging_setup.py` | one hit, `(level: str = "INFO")` | `:27`, `def configure_logging(level: str = "INFO") -> None:` | ✅ |
| 5 | `log_level` in `web/settings.py` | no hit | no hit (grep exit 1) | ✅ |
| 6 | `grep -c 'not paired'` in the section template | `2` | `2` | ✅ |
| 7 | `"telegram_pairing": _pairing_view` | one hit in `_render_section` | `:682` | ✅ |
| 8 | `OPENROUTER_API_KEY is empty` | one hit in each file | `bot/config.py:165`, `tests/bot/test_telegram_bot.py:198` | ✅ |
| 9 | `^_bot_tasks` in `bot/telegram_bot.py` | one hit | `:184` | ✅ |
| 10 | `grep -c 'TELEGRAM_BOT_ENABLED'` in the deploy doc | `1` | `1` | ✅ |
| 11 | `def test_bot_disabled_is_noop` | one hit | `:76` | ✅ |
| 12 | `pytest tests/bot tests/web/test_provider_credentials.py -q` | green | **168 passed** in 128.95s, 0 skipped (dev Postgres up) | ✅ |

**Note on the `.env.example` pointer.** §3.3.3 cites "line ~947 of the file";
`.env.example` is 279 lines and the comment block above `TELEGRAM_BOT_ENABLED` sits at
**104–116**. The 947 is a Repomix-bundle figure, the same class of offset recorded for
earlier prompts. The block matched the prompt's description exactly, so this is a
pointer discrepancy, not a content mismatch — no STOP.

---

## 3. What changed

| File | +/− | What |
|---|---|---|
| `web/settings.py` | +43 / −0 | `log_level: str = "INFO"` (reads `LOG_LEVEL`) plus `_validate_log_level` against `_VALID_LOG_LEVELS`, mirroring `core.config`: rejected, never degraded, and normalised to upper case. |
| `web/main.py` | +18 / −1 | `from core.logging_setup import configure_logging`; `run()` calls `configure_logging(settings.log_level)` before `uvicorn.run(...)`, with the docstring saying why it is here and not in `create_app`/lifespan. |
| `bot/telegram_bot.py` | +92 / −1 | `_bot_bindings` next to `_bot_tasks`, filled as each dispatcher registers, cleared in the worker's `finally` and in `stop_bot`; frozen `BotStatus(env_enabled, running, tenant_served)` + `bot_status(tenant_id)`; both exported in `__all__`. No aiogram import, no lock. |
| `web/routes/provider_credentials.py` | +11 / −0 | `_render_section` adds `"telegram_bot": bot_status(session.tenant_id)` via a lazy import, mirroring the lifespan's `from bot.telegram_bot import start_bot`. `_pairing_view` untouched. |
| `web/templates/_partials/provider_credentials_section.html` | +36 / −0 | The four-state status line under the card `<header>`; the pairing actions wrapped in `{% if telegram_bot.env_enabled %}`; context docstring extended. |
| `bot/config.py` | +30 / −21 | The two start-up lines become **INFO** and state what is true; the surrounding comment and four docstring phrases follow. |
| `.env.example` | +3 / −0 | One sentence on the tenant `Enabled` field versus the master switch. |
| `docs/deploy/telegram-multi-bot.md` | +27 / −0 | `### Two switches` at the start of §1 (table + the four card states) and the INFO-lines note after the start-up-log block. |
| `tests/bot/test_telegram_bot.py` | +129 / −11 | Four new `bot_status` / binding tests; the two config tests renamed and re-asserted at INFO against the new substrings; `_bot_bindings` added to the reset fixture. |
| `tests/web/test_provider_credentials.py` | +111 / −1 | Autouse `_bot_status_serving` fixture, four verbatim copy constants, `_card_copy` helper, the parametrised four-state test and the withdrawn-actions test. |
| `tests/web/test_web_run_logging.py` | **new**, 72 lines | 3 tests: call order + level, an unknown level refused, a lower-case level normalised. |

### Test counts

| Suite | Before | After |
|---|---|---|
| `tests/bot` | 108 | **112** (+4) |
| `tests/web/test_web_run_logging.py` | — | **3** (new file) |
| `tests/web/test_provider_credentials.py` | 60 | **65** (+5: 4 parametrised states + 1 withdrawn-actions) |

### Gates

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 946 files already formatted (two files were reformatted once, then clean) |
| `pyright` (bare, per `[tool.pyright] include`) | **0 errors**, 0 warnings |
| `pytest tests/bot tests/web/test_web_run_logging.py -q` | **115 passed** in 3.79s |
| `pytest tests/web/test_provider_credentials.py -k "pairing or bot_state or code or revok or panels"` | **20 passed**, 45 deselected, 39.09s |
| `pytest tests/web/test_provider_credentials.py -q` (full file) | **65 passed** in 182.73s |
| Full suite (`tests/`) | not run — out of scope per §4 of the prompt |

The targeted selection ran first because it covers exactly the risk the template change
carries: all ten pre-existing pairing tests plus the five new ones, and one non-Telegram
render (`test_owner_sees_both_panels_with_consumer_pills`) to confirm the new context key
breaks no other card. The whole file followed and is green, so that selection is now
subsumed.

---

## 4. D-register

Four states, one line each, chosen in the order of what blocks first. Final copy as
rendered (the environment variable is set in `<code>`, which the note already styles):

| ID | State | Copy |
|---|---|---|
| **D-TG-1** | `not env_enabled` | The Telegram bot is switched off for this deployment (`TELEGRAM_BOT_ENABLED`). Pairing is not possible until the operator enables it. |
| **D-TG-2** | `env_enabled and not running` | The Telegram bot is enabled but not running. Check the web process start-up log. |
| **D-TG-3** | `running and not tenant_served` | No bot is running for this tenant. An owner stores a bot token under Tenant credentials; token changes apply after a restart. |
| **D-TG-4** | `tenant_served` | The bot for this tenant is running. |

In the D-TG-1 state the `Generate pairing code` form and the revoke form are not
rendered; the paired/not-paired pill and the "Chat … is linked" note stay, so an
existing pairing remains visible. The pairing *endpoints* are unchanged.

---

## 5. Decisions taken inside the strand

Five judgement calls the prompt did not settle. None changes the shape of §3.

**5.1 `bot_status` survives a bot config that will not build.** `get_bot_config()`
raises `ConfigurationError` on an enabled bot with, say, a malformed
`TELEGRAM_ALLOWED_USER_IDS` — a state a *working* web process can be in. Letting that
propagate would turn a bot-config typo into an HTTP 500 on the whole Providers &
Credentials section. `bot_status` catches it and reports `env_enabled=False`: a
configuration that will not build is a bot that cannot run (`start_bot` calls the same
constructor), and the reason is in the start-up log. Documented at the call site.

**5.2 The `SHIRLEY_MODEL` line was reworded too.** §3.3.1 gives the new `OPENROUTER_API_KEY`
copy and says "same shape for `SHIRLEY_MODEL`", but §3.3.2 names only the API-key
assertion. Both messages are now INFO, so the model test's `caplog.at_level(WARNING)`
assertion would have failed — it is updated on the same terms. Both tests now also
assert the record's `levelno`, so a silent relapse to WARNING fails.

**5.3 An autouse fixture keeps the existing pairing tests meaningful.**
`tests/web/conftest.py` forces `TELEGRAM_BOT_ENABLED=false` for the whole package (a live
aiogram bot in a test process steals the production bot's update stream), so the card's
*real* state in every web test is D-TG-1 — which withdraws the pairing actions and would
have failed the ten pre-existing pairing tests. `_bot_status_serving` patches the seam to
the healthy state for the file, and the five state tests patch it themselves. The
fixture's docstring records why.

**5.4 The actions block is not re-indented.** The `{% if telegram_bot.env_enabled %}`
wrapper sits at the `<div class="pf-credentials__actions">` indentation rather than
indenting the twenty lines inside it. §3.2 asks for an additive template change the UX
track can rebase over; a whitespace-only reflow of the block would have been the opposite.

**5.5 No CSS.** `pf-credentials__note--bot` is a hook with no rule of its own, per §3.2
("reuse `pf-credentials__note` styling"); `.pf-credentials__note code` already styles the
inline variable name. Styling beyond this is the UX track's.

---

## 6. Open questions

None that block the commit.

One observation for a later prompt, already out of scope per §5 of the prompt: the
status line explains *why* pairing is unavailable, but nothing on the surface tells an
owner that a token they just stored needs a restart to be discovered — D-TG-3 says
"token changes apply after a restart" only once no bot serves the tenant. The
restart-to-apply rule is deploy-doc knowledge; a `cli/status.py` check ("token stored,
switch off" / "token stored since the last restart") is the candidate home, as §5 notes.
