# ADR-0030: Telegram Bot as First Non-GUI Client of Headless Shirley

- **Status:** Accepted
- **Date:** 2026-04-29
- **Deciders:** PortfoliFLOW project owner
- **Tags:** integration, architecture, security

---

> **Note (2026-05-03, Phase 1, stream A2):** The Telegram bot remains the
> first non-GUI Shirley client, but its consumed seam is now
> `services.ai_service_core.AIServiceCore.stream_response` (per ADR-0038
> stream A2). `services/headless_shirley.py` has been removed; references
> to it below are preserved as the historical record of the original
> decision. The bot's whitelist, configuration, lifecycle, layering, and
> privacy posture commitments — the load-bearing decisions of this ADR —
> are unchanged.

## Context

ADR-0029 introduced `services/headless_shirley.run_turn` as a Qt-free,
synchronous entry point to Shirley's tool-execution loop. The motivation
was a concrete pending consumer: a Telegram bot that lets the operator
talk to Shirley from a phone, away from the desk, while reusing the
existing DataStore, ToolRegistry, and AI configuration that the GUI runs
against.

A bot is a small architectural commitment with several real questions
that need to be decided rather than left implicit:

1. **Process placement.** In-process daemon thread, separate executable
   over IPC, or external service over HTTP?
2. **Layering.** Where does `bot/` sit in the dependency graph, and
   what may it import?
3. **Installability.** Is aiogram a hard dependency for everyone or an
   opt-in extra for users who actually run the bot?
4. **Authentication.** What stops an arbitrary Telegram user from
   talking to Shirley?
5. **Authorisation.** What can an authenticated user actually do — read
   only, or full tool parity with the GUI?
6. **Privacy.** Telegram cloud chats are not end-to-end encrypted; what
   classes of data may flow through them?
7. **Failure mode.** What happens when bot configuration is wrong, the
   network is unstable, or aiogram raises in a worker thread?

PortfoliFLOW is single-user today and the operator is the only person
who will use the bot for the foreseeable future. The decisions below
optimise for that reality while preserving every option that a future
multi-user or client-server topology will need.

This decision is integration-, architecture-, and security-relevant
(BAIT AT 7.2 — IT-risk management for an automated execution surface;
KAGB / BaFin AVV considerations for any future client-data flow).

## Decision

PortfoliFLOW introduces a new top-level package, `bot/`, whose three
files (`bot/__init__.py`, `bot/config.py`, `bot/telegram_bot.py`) define
an opt-in Telegram bot. The bot is the first concrete consumer of
`services.headless_shirley.run_turn` (ADR-0029). It is wired to the
application as follows:

**Process placement.** The bot runs in-process, on a daemon thread, on
its own asyncio event loop. `start_bot()` spawns the thread; the thread
creates a fresh `asyncio` loop, instantiates `aiogram.Bot` and
`aiogram.Dispatcher`, registers a single text handler, and starts
polling. `stop_bot()` is idempotent and is wired to `app.aboutToQuit`
in `main.py`.

**Layering.** `bot/` may import from `core/` and `services/` only. It
must not import from `gui/` or `modules/`. This is a hard rule, codified
in `CLAUDE.md`'s "Dependency rules" section. The Qt-free invariant of
ADR-0029 is enforced from the bot side too: the regression-guard test
`tests/bot/test_telegram_bot.py::test_no_qt_import` imports
`bot.telegram_bot` in a fresh subprocess and asserts that `"PyQt6"` is
not present in `sys.modules` afterwards.

**Installability.** aiogram is declared as an *optional* dependency
under the `bot` extra in `pyproject.toml`. Users who do not run the bot
do not pay for it. The aiogram import inside `bot.telegram_bot` is lazy
(inside `_run_bot_in_thread`) so importing `bot.telegram_bot` itself
does not require aiogram to be installed; the regression-guard test
relies on this. The ADR records the *pattern* (optional extra, not
core) — the version constraint lives in `pyproject.toml` and is
allowed to drift independently of this ADR.

**Activation.** The bot is opt-in via `.env`:

| Variable                    | Required when enabled | Purpose                                      |
|-----------------------------|-----------------------|----------------------------------------------|
| `TELEGRAM_BOT_ENABLED`      | always read           | Master switch (`true` to enable)             |
| `TELEGRAM_BOT_TOKEN`        | yes                   | Bot token from BotFather                     |
| `TELEGRAM_ALLOWED_USER_IDS` | yes (non-empty)       | Comma-separated whitelist of Telegram IDs    |
| `OPENROUTER_BASE_URL`       | optional (defaults)   | OpenAI-compatible endpoint                   |
| `OPENROUTER_API_KEY`        | yes                   | API key for the endpoint                     |
| `SHIRLEY_MODEL`             | yes                   | Model ID for `run_turn`                      |

When `TELEGRAM_BOT_ENABLED=true` with any required field empty,
`BotSettings.__post_init__` raises `ConfigurationError` immediately. A
disabled bot tolerates any combination of empty fields. An enabled bot
with an empty whitelist is refused at config-load time — there is no
"open to everyone" mode.

**Authentication.** Whitelist-only. `bot.config.BotSettings` parses the
`TELEGRAM_ALLOWED_USER_IDS` value into a `frozenset[int]` at construction
time. The handler in `bot.telegram_bot._handle_text_message` checks
`user.id in config.allowed_user_ids` for every inbound message and
**drops** non-whitelisted messages silently (with a WARNING log entry).
Replying "you are not authorised" is rejected because it would leak
the bot's existence to the wider Telegram user base.

**Authorisation surface.** Today the bot has full tool parity with the
GUI. Both read-only tools and any future write or external-effect tools
are reachable. This is a deliberate single-operator choice for the
testing phase: filtering tools at the bot's seam before the operator
has hands-on experience with the bot would risk debugging artificially
introduced gaps. A bot-side trust-class filter is named here as a
follow-up that becomes mandatory before any third-party use of the bot;
see ADR-0019 (multi-user readiness) and ADR-0022 (tool trust classes).

**Failure containment.** `main.py` wraps `start_bot()` in a `try/except`
that catches both `ConfigurationError` and any unexpected exception.
Bot failures must never block GUI startup, because the bot is opt-in
and orthogonal to the GUI's critical path.

**Privacy posture.** Telegram cloud chats are not end-to-end encrypted.
Messages traverse Telegram's servers and are stored under Telegram's
operational policies. This is acceptable for the operator's own
observation and test data but not for client (Mandanten) data under
BaFin / KAGB rules. PortfoliFLOW records this as a known limitation,
not a solved problem. The architectural answer is that
`headless_shirley.run_turn` is channel-agnostic: switching the bot
implementation to Signal, Threema, or a custom mobile / web client is
an additive change (a sibling package, identical call site), not a
rewrite.

**Migration path.** The bot is a sibling to the GUI today; in the
planned client-server world (ADR-0018), it becomes a client of the
FastAPI surface that wraps `headless_shirley`. This ADR does not
commit to FastAPI here, but it commits to keeping the bot agnostic to
in-process versus over-the-wire transport: the bot's call site is a
single function call to `run_turn`, with no in-process-only assumptions.

## Rationale

- **In-process / daemon-thread placement gives Shirley first-class
  access to the running state.** The DataStore, ToolRegistry, AI
  configuration, and chart-theme cache are all in-process singletons.
  Any IPC layer between the bot and Shirley would either re-implement
  those (drift) or re-fetch them per turn (latency). The bot is the
  same process; it pays no price for that proximity.
- **Daemon-thread is the right discipline for an opt-in subsystem.**
  A daemon thread cannot block process exit. If the operator quits
  the GUI while the bot is mid-poll, the process tears down cleanly.
- **`bot/` as a top-level package, not a module under `services/`,
  reflects what it is.** A messenger bot is a *channel*, not a
  service. Placing it under `services/` would conflate it with
  cross-cutting integrations (AI, web research, reporting). A peer
  package with its own dependency rule keeps the layering legible.
- **Optional `bot` extra is fail-safe.** A user who runs `pip install -e .`
  without the extra has a working PortfoliFLOW with no aiogram in the
  import graph, and `bot.telegram_bot` still imports cleanly because
  the aiogram import is deferred to `_run_bot_in_thread`. The Qt-free
  regression-guard test exercises exactly this path.
- **Whitelist-only authentication is the simplest credible primitive.**
  A Telegram bot is an open execution surface to the public internet.
  Without a whitelist guard rail, the bot would expose an LLM-powered
  execution surface (with full tool parity) to anyone who finds it.
  The whitelist is enforced at config-load time (refuse to start with
  no whitelist) and at every inbound message (silent drop for
  non-whitelisted users).
- **Silent drop for non-whitelisted users is a deliberate
  existence-leak mitigation.** Replying "not authorised" tells a
  random Telegram user that this bot exists and accepts messages.
  Dropping silently does not.
- **Failure containment in `main.py` is the right shape for an
  orthogonal subsystem.** The GUI is the operator's critical path;
  the bot is a convenience. A misconfigured `TELEGRAM_BOT_TOKEN` must
  not stop the GUI from starting.
- **Naming the privacy posture is honest, not obstructive.** Telegram
  is fine for the operator's test data today. Stating it openly here
  prevents a future reader from assuming a privacy guarantee that does
  not exist.
- **Recording the migration path now keeps it cheap later.** When
  ADR-0018 reaches implementation, the bot's call site is the same
  whether `run_turn` is an in-process function or wraps an HTTP call
  to a FastAPI endpoint — because the bot only knows about the
  function signature, not about transport.

## Alternatives Considered

- **Run the bot as a separate process, communicating over IPC.**
  Rejected for this iteration: adds an IPC surface (sockets, named
  pipes, or HTTP) before there is a real reason for one; loses
  direct access to the in-process DataStore and ToolRegistry; multiplies
  the deployment complexity for a single-user setup.
- **Build a FastAPI server first and have the bot talk HTTP from day
  one.** Rejected — premature; the bot would carry the cost of a
  server boundary while having no second client to justify it. The
  migration path is preserved (ADR-0018), not pre-paid.
- **Use a polling cron loop instead of an aiogram-style long-polling
  client.** Rejected — a maintained Bot-API client is the standard
  solution; reinventing it is needless surface area, especially
  around message delivery, retry, and update offsets.
- **Allow `bot/` to import from `gui/` for shared widgets like the
  system-prompt loader.** Rejected — would cross the layer boundary
  and pull PyQt6 into the bot's import graph, breaking the Qt-free
  guarantee that `headless_shirley` rests on. The duplicate
  system-prompt loader in `bot/telegram_bot.py::_load_system_prompt`
  is the cost of that decision and is documented in the module
  docstring.
- **Open the bot to the public with a generic "ask me anything"
  mode.** Rejected — without the whitelist guard rail, the bot would
  expose an LLM-powered execution surface to the open internet.
- **Filter tools at the bot's seam from day one (read-only mode).**
  Rejected for this iteration — see *Authorisation surface* above. A
  filter introduced before the operator has used the bot in anger
  risks debugging artefacts that exist only because of the filter.
  Recorded as a precondition for any future third-party rollout.
- **Make aiogram a hard dependency.** Rejected — the operator may
  ship PortfoliFLOW to environments where the Telegram channel is
  irrelevant (e.g. a future Signal-only build). An optional extra
  matches the orthogonality of the channel.

## Consequences

### Positive

- The first non-GUI consumer of Shirley is shipped, validated, and
  honours the Qt-free invariant of ADR-0029 from the import side.
- The dependency rule for `bot/` is explicit and enforced by a
  regression test, not by convention.
- Configuration validation fails loudly at startup; misconfigured
  deployments cannot silently fall back to "no whitelist" or "no
  token".
- The architecture is ready for a second channel (Signal, Slack, or
  HTTP) — a sibling package reusing `run_turn` at the same call site.
- Bot failures are isolated from the GUI's critical path.

### Negative

- The bot has full tool parity with the GUI today, including any
  future write tools. This is acceptable for the single-operator
  testing phase but is the largest open item before any third-party
  use; named here, not glossed over.
- Telegram's privacy posture limits the data classes that may flow
  through the bot. For client (Mandanten) data under BaFin / KAGB,
  the architecture supports a switch to a different channel but does
  not solve the data-flow question.
- The system-prompt loader in `bot/telegram_bot.py` is duplicated
  from `services.ai_service.AIService.get_system_prompt` for the
  same Qt-free reason that drives ADR-0029's loop duplication. The
  cost is small (file-format-driven parsing) but real.
- Concurrent GUI and bot turns are not fully serialised against each
  other; see ADR-0031 for the scope and limitation of the lock.
- The `bot*` glob is not yet listed in `[tool.setuptools.packages.find]`
  in `pyproject.toml`. Editable installs are unaffected; wheel builds
  would omit `bot/`. This is a packaging gap to fix in a separate
  code change, recorded here for traceability but not part of this
  ADR's scope.

### Neutral / Follow-ups

- **Authorisation surface.** Introduce a tool-class filter at the
  bot's seam before any third-party rollout. Default proposal:
  `READ_INTERNAL` and `READ_EXTERNAL_UNTRUSTED` only; differentiated
  per-user roles when ADR-0019 reaches implementation.
- **Authentication beyond whitelist.** A pairing-token flow (operator
  generates a one-time token in the GUI; the user enters it in
  Telegram once; the token has a 30-day lifetime) becomes appropriate
  before the first external user.
- **Channel agnosticism.** A second messenger (Signal preferred for
  client-data scenarios) is an additive package, not a rewrite. Defer
  until there is a concrete data class that demands it.
- **Multi-turn conversations per chat.** Today every message is a
  one-shot `run_turn(conversation=None)`; Shirley has no memory across
  Telegram messages. A `dict[chat_id, Conversation]` in
  `bot.telegram_bot` is the obvious next step, with a `/clear`
  command and an idle-expiry policy. Out of scope for this ADR.
- **Markdown rendering.** Replies are sent as plain text today.
  Telegram's `MarkdownV2` is an option once the operator's daily
  usage produces enough long-formatted responses to justify the
  escape-handling cost.

## Implementation Notes

- Package: `bot/__init__.py`, `bot/config.py`, `bot/telegram_bot.py`.
- Public API: `bot.telegram_bot.start_bot`, `bot.telegram_bot.stop_bot`.
- Configuration: `bot.config.BotSettings`, `bot.config.get_bot_config`.
  Validation logic is in `BotSettings.__post_init__`; whitelist
  parsing is in `BotSettings._parse_whitelist`.
- Wiring: `main.py` (`try/except` block around `start_bot()`;
  `app.aboutToQuit.connect(stop_bot)`).
- Optional dependency: `aiogram>=3.0` under the `bot` extra in
  `pyproject.toml`. Install via `pip install -e ".[bot]"`.
- Environment variables: `.env.example` lists `TELEGRAM_BOT_ENABLED`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`,
  `OPENROUTER_BASE_URL`, `OPENROUTER_API_KEY`, `SHIRLEY_MODEL`.
- Tests: `tests/bot/test_telegram_bot.py` — covers `start_bot` /
  `stop_bot` lifecycle, `BotSettings` validation, the whitelist
  parser, the long-message splitter, and the Qt-free regression
  guard `test_no_qt_import`.
- Whitelist-drop log line: `bot/telegram_bot.py::_handle_text_message`,
  WARNING level, recording the rejected `user_id` and a 50-character
  prefix of the rejected text for diagnostic purposes.

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Security (whitelist
  authentication, fail-loud config validation, silent drop of
  unauthenticated traffic to avoid existence-leak), Maintainability
  (additive layer, no modifications to GUI or modules), Portability
  (channel-agnostic call site ready for second messenger or HTTP
  transport).
- **Regulatory references:** BAIT AT 7.2 — the bot is an automated
  execution surface and the controls (whitelist, fail-loud config,
  isolation from GUI startup) are the kind of structural mitigation
  AT 7.2 expects to see for such a surface. KAGB / BaFin AVV — any
  future client-data flow through Telegram is recorded as a known
  constraint, not a solved problem; the channel-agnosticism of
  `headless_shirley` is the architectural answer when that constraint
  binds.
- **Audit evidence:** `bot/config.py::BotSettings.__post_init__`
  validation logic; `tests/bot/test_telegram_bot.py` (the
  configuration-validation tests and `test_no_qt_import`); the
  `try/except` block in `main.py` around `start_bot()`; the
  whitelist-drop WARNING log line in
  `bot/telegram_bot.py::_handle_text_message`; this ADR.

## References

- ADR-0001 (Layered architecture and strict one-way dependencies —
  the dependency rule for `bot/` is the same shape as for
  `analytics/` and `services/`)
- ADR-0019 (Planned multi-user readiness — bot authentication
  beyond whitelist and per-user authorisation are multi-user
  follow-ups)
- ADR-0022 (Tool trust classes and gating policy — the filter the
  bot's seam will eventually apply)
- ADR-0029 (Headless Shirley — the Qt-free entry point this bot
  consumes)
- ADR-0031 (Module-level threading lock — the concurrency control
  that protects the bot path's turns against each other)

---

## Revision History

| Date       | Author                       | Change                                                              |
|------------|------------------------------|---------------------------------------------------------------------|
| 2026-04-29 | PortfoliFLOW project owner   | Initial draft. Records the decision behind the `bot/` package, its layering, opt-in installability, whitelist authentication, privacy posture, and migration path to a future client-server topology. Code already implemented and in use. |
| 2026-05-03 | PortfoliFLOW project owner   | Phase 1, stream A2 (ADR-0038): the bot's consumed seam moved from `services.headless_shirley.run_turn` to `services.ai_service_core.AIServiceCore.stream_response`. The bot constructs its own `AIServiceCore` instance (separate from the GUI singleton) so its `.env`-driven configuration cannot collide with the GUI's `QSettings`-driven configuration; the shared `ToolRegistry` and the relocated process-wide `_TURN_LOCK` continue to enforce ADR-0022 gating and ADR-0031 serialisation across both cores. Whitelist, configuration validation, lifecycle, layering, and privacy posture are unchanged. The bot remains the first headless client of the system. Decider: PortfoliFLOW project owner. |
| 2026-05-10 | PortfoliFLOW project owner   | Translated residual German passages to English per ADR-0008 (Phase-6 Block 0c). No substantive change; status, decisions, and content unchanged. |
