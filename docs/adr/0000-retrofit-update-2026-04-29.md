# ADR-0000 Update: Retrofit Follow-up — 2026-04-29

- **Status:** Informational (not a decision)
- **Date:** 2026-04-29
- **Author:** PortfoliFLOW project owner
- **Tags:** process, meta

---

## Summary

This is the second follow-up to the original retrofit report
([`0000-retrofit-report.md`](./0000-retrofit-report.md), 2026-04-24)
and the first follow-up to the 2026-04-27 retrofit update
([`0000-retrofit-update-2026-04.md`](./0000-retrofit-update-2026-04.md)).
Between 2026-04-27 and 2026-04-29 the project shipped two new
subsystems whose ADRs had not yet been written, and ran a UI-polish
thread that produced a schema-extension decision whose code is
deliberately *not* yet written. This update formalises both — three
`Accepted` ADRs whose code is in the working tree and one `Proposed`
ADR whose implementation is future work — and records two
Revision-History rows on existing ADRs whose relationship to the new
subsystems is now load-bearing.

The previous update report (2026-04-27) and the original retrofit
report (2026-04-24) are **not** edited. Their text continues to
describe the state of the codebase at the moments they were written.
This update is the authoritative record of what changed since
2026-04-27.

## Trigger

Three threads of work converged on this update:

1. **Etappe 1.A — `services/headless_shirley.py`.** A Qt-free,
   synchronous entry point to Shirley's tool-execution loop, designed
   to serve non-GUI consumers. Built between 2026-04-27 and
   2026-04-28. ADR-0029 records the decision.
2. **Etappe 1.B — `bot/` (Telegram bot).** The first concrete consumer
   of `headless_shirley.run_turn`. The bot was first live-tested on
   2026-04-28; the test was successful. ADR-0030 records the
   decision.
3. **UI-polish thread (2026-04-29).** A QSS-only polish session that
   surfaced the next layer of work — extending the JSON theme schema
   with `layout`, `pill`, and an expanded `font` section. The
   decision was reached but the schema extension itself is future
   work. ADR-0032 records the decision as `Proposed`.

In addition, `_TURN_LOCK` in `services/headless_shirley.py` was
recognised as a separate, security-relevant decision deserving its own
ADR. It is recorded as ADR-0031 (Accepted), distinct from ADR-0029
which records the entry-point decision.

## Source documents

This update was based on two operator-supplied handover documents
that were read end-to-end before any ADR was drafted:

- `handover_telegram_bot_offene_punkte.md` — the operator's record of
  open points after the successful Telegram-bot live test, organised
  by recommended bearbeitungs-zeitpunkt. Block A.4 of that document
  is the explicit prompt for the three Accepted ADRs in this update.
- `handover_ui_polish.md` — the operator's record of the UI-polish
  thread, including its Section 3 (recommended next steps in priority
  order) and Section 6 (open question on whether `density` is in
  scope for the first schema-extension ADR). Section 6's
  recommendation — *defer density* — is recorded in ADR-0032 as the
  decision, with the reasoning that the handover lays out.

The implementation files that anchor each Accepted ADR were also read
end-to-end:

- For ADR-0029: `services/headless_shirley.py`,
  `tests/services/test_headless_shirley.py` (notably
  `test_no_qt_import_in_fresh_subprocess` and
  `test_lock_serialises_concurrent_turns`),
  `services/ai_service.py::_StreamWorker.run`.
- For ADR-0030: `bot/__init__.py`, `bot/config.py`,
  `bot/telegram_bot.py`, `tests/bot/test_telegram_bot.py` (notably
  `test_no_qt_import` and the configuration-validation tests),
  `main.py`'s `try/except` wiring around `start_bot()`,
  `pyproject.toml` (the `bot` extra), `.env.example`.
- For ADR-0031: `services/headless_shirley.py` ("Concurrency"
  docstring section and `_TURN_LOCK`),
  `services/tool_registry.py` (the `_locked_classes: set[ToolClass]`
  field and `begin_turn` / `end_turn` / `execute_tool`),
  `services/ai_service.py::_StreamWorker.run` (no lock acquisition).

For ADR-0032, the references read for context were
`config/ui_theme.json`, `config/ui_theme_light.json`,
`config/ui_theme_corporate_blue.json`, `core/ui_theme.py`, and
`gui/theme.py`. None of these is modified by this update; ADR-0032
describes their *future* state.

## New ADRs

Four ADRs are added in this update. The Accepted-vs-Proposed
distinction is load-bearing for an audit reader: three of them
formalise code that exists, the fourth records a future decision.

- **ADR-0029 — Headless Shirley as Qt-Free Synchronous Entry Point
  for Non-GUI Clients.** *Accepted.* Records the existence of
  `services/headless_shirley.run_turn` as the Qt-free synchronous
  entry point to Shirley's tool-execution loop, the deliberate
  duplication against `services.ai_service._StreamWorker.run`, the
  named unification follow-up (callback-based pure function
  refactor, target window: after the bot has stabilised in
  production use, before the planned client-server refactor of
  ADR-0018), and the Qt-free invariant enforced by
  `tests/services/test_headless_shirley.py::test_no_qt_import_in_fresh_subprocess`.
- **ADR-0030 — Telegram Bot as First Non-GUI Client of Headless
  Shirley.** *Accepted.* Records the `bot/` package, its
  in-process / daemon-thread placement, the `core/ + services/` only
  dependency rule, the optional `bot` extra in `pyproject.toml`,
  whitelist-only authentication, the silent-drop existence-leak
  mitigation, fail-loud configuration validation, the deliberate
  full-tool-parity authorisation surface for the testing phase
  (with bot-side trust-class filtering recorded as a precondition
  for any future third-party rollout), the named privacy posture
  (Telegram is fine for the operator's test data; not for
  Mandanten data under BaFin / KAGB), and the migration path to a
  future FastAPI surface.
- **ADR-0031 — Module-Level Threading Lock as Interim Concurrency
  Control for Bot-Side Turns.** *Accepted.* Records the
  `_TURN_LOCK` in `services/headless_shirley.py`, what it
  protects (bot-vs-bot serialisation of the unsynchronised
  `ToolRegistry._locked_classes`), what it explicitly does not
  protect (bot-vs-GUI concurrent turns through
  `_StreamWorker.run`), and the named fix path (full thread-safety
  of `ToolRegistry`, bundled with the planned ADR-0018 refactor at
  which point the registry's lifecycle is reconsidered anyway).
  The residual race is named honestly, not glossed over.
- **ADR-0032 — UI Theme Schema Extension for Layout, Pill, and
  Font Tokens.** *Proposed.* Records the decision to extend
  `config/ui_theme*.json` with `layout`, `pill`, and an expanded
  `font` section under the same externalisation pattern as
  ADR-0025; the explicit deferral of `density` to a later ADR; the
  rationale for placing `tabular_figures` in `font` (not
  `layout`) and applying it via the QFont API at app startup
  (not via QSS); and the lockstep update obligation across the
  three shipped theme variants. No code yet implements the
  extension; the *Implementation Notes* section explicitly marks
  every affected path `(planned)`.

## Existing ADRs touched

Revision-History row only — no body rewrites. Each gained exactly one
new row dated 2026-04-29.

- **ADR-0017 (Planned DataVault — DuckDB-backed persistent layer
  with audit fields).** Status unchanged (`Proposed`). Revision
  History row added: cross-reference note that the Qt-free
  `services/headless_shirley.py` (ADR-0029) is the architectural
  precondition this ADR's eventual implementation will sit behind,
  and that ADR-0031 names the concurrency follow-up that becomes
  resolvable once the DataVault replaces the in-memory DataStore.
- **ADR-0018 (Planned Service / Repository layering as
  prerequisite for client-server migration).** Status unchanged
  (`Proposed`). Revision History row added: cross-reference note
  that ADR-0029 has delivered the first Qt-free service entry
  point, partially anticipating the service / repository split
  this ADR describes; the remaining work is unchanged.

**ADR-0025 is intentionally not amended.** Although ADR-0032
extends ADR-0025 with new schema sections, ADR-0032 is `Proposed`
and Proposed ADRs do not retroactively modify their parents. Once
ADR-0032 reaches `Accepted` and the schema extension lands, a
Revision-History row on ADR-0025 will be appropriate at that point.
Until then, ADR-0025 remains as it was on 2026-04-27.

## Cross-document changes

Per-prompt scope, two non-ADR files received small targeted edits:

- **`CLAUDE.md`** — confirmed the bot sentence in the
  "Architecture in one paragraph" section and the `bot/`
  dependency-rule bullet in the "Dependency rules" section
  (both already present, added during bot implementation);
  appended new lines to "Current project status" for
  `services/headless_shirley.py` and `bot/`; added two new
  sub-sections under "Implemented Services Reference" for
  Headless Shirley and the Telegram Bot, each cross-referencing
  the relevant new ADR; appended a new sub-section under
  "Planned Architecture (Not Yet Implemented)" for the UI Theme
  Schema Extension cross-referencing ADR-0032; added an
  ADR-0029 / ADR-0030 / ADR-0031 cross-reference at the
  appropriate points in line with the existing
  `(see ADR-NNNN)` style. Added one bullet to the "What not to
  do — ever" list forbidding `bot/` from importing `gui/` or
  `modules/`. No other content changed.
- **`docs/architecture.md`** — added `bot/` to the
  *Dependency rules* diagram bullet list (mirroring the
  `analytics/` style); added a new
  ``### `bot/`` sub-section under *Layer responsibilities*
  cross-referencing ADR-0029 and ADR-0030; appended a
  *Cross-cutting services* bullet for the Qt-free Shirley entry
  point cross-referencing ADR-0029, ADR-0030, ADR-0031.
  Nothing about the planned UI theme schema extension was added
  here — that belongs in `CLAUDE.md`'s Planned Architecture
  section, not in the architecture document, until the schema
  is actually implemented.

No code under `core/`, `services/`, `modules/`, `gui/`,
`analytics/`, `bot/`, or `tests/` was modified in this update.

## Out of scope (deliberate)

- **`readme.md`.** Will be revised in a separate session after the
  ADR and `CLAUDE.md` work has settled.
- **`pyproject.toml`.** The `bot*` glob is missing from
  `[tool.setuptools.packages.find]`. This is a packaging gap noted
  in the operator's handover (item A.2) and will be picked up in a
  separate code change. Documenting it in ADR-0030 was intentional;
  fixing it in `pyproject.toml` was not in scope here.
- **The schema extension itself.** ADR-0032 is `Proposed`. The JSON
  schema changes, the loader validation extension, the
  `apply_application_font` helper, and the widget-code migration
  from hardcoded magic numbers are all left to future sessions.
- **Any other ADR.** ADR-0025 is intentionally untouched. No
  status field of any other ADR changes.

## Debts acknowledged but not paid here

The handover document `handover_telegram_bot_offene_punkte.md`
identifies three deliberate debts that were taken on during the
implementation of `headless_shirley` and the Telegram bot. They are
named in the relevant new ADRs (0029, 0030, 0031) but worth
re-stating here so the audit reader sees the linkage in one place:

1. **Code duplication between
   `services.ai_service._StreamWorker.run()` and
   `services.headless_shirley.run_turn()`.** Recorded in ADR-0029.
   Tilgung: refactor the loop into a callback-based pure function
   (`services/_tool_loop.py` is the candidate path named in the
   handover; the path itself is *not* invented as evidence in
   ADR-0029). Target window: after the bot has stabilised in
   production use, before the planned client-server refactor of
   ADR-0018.
2. **System-prompt loader duplication between
   `services.ai_service.AIService.get_system_prompt()` and
   `bot.telegram_bot._load_system_prompt()`.** Recorded in
   ADR-0029 (the cause is the Qt-free invariant) and in
   `bot/telegram_bot.py`'s module docstring. Tilgung: bundled with
   the same refactoring wave as the loop unification — the cause
   is identical, so the fix should be too.
3. **Cross-channel concurrency gap on `ToolRegistry`.** Recorded
   in ADR-0031. The `_TURN_LOCK` closes the bot-vs-bot case; the
   bot-vs-GUI case is the residual race. Tilgung: full
   thread-safety of `ToolRegistry`, bundled with the planned
   ADR-0018 refactor.

In addition:

4. **Planned-but-unimplemented status of ADR-0032.** The schema
   extension itself, the schema-version bump in each variant, the
   `core/ui_theme.py` validation extension, the
   `apply_application_font` helper, and the widget-code migration
   are all future work tracked in ADR-0032 and in
   `handover_ui_polish.md` Section 3. They are not part of this
   documentation pass.

## Re-evaluation of the carried-forward gap list

The 2026-04-27 update report carried forward 13 numbered open
gaps from the original retrofit. None of them is closed by this
update. Two of them gain new context:

- **Gap 1 (Authentication strategy).** ADR-0030 introduces a
  whitelist-only Telegram authentication primitive. This is a
  *narrow* primitive for a *single* channel; it does not
  constitute the general authentication strategy ADR. The full
  decision remains open and ADR-0030 explicitly cross-references
  ADR-0019 for the multi-user follow-up.
- **Gap 2 (Authorisation / RBAC model).** ADR-0030 records the
  bot's full-tool-parity authorisation surface as a deliberate
  single-operator choice for the testing phase, with bot-side
  trust-class filtering named as a precondition for any future
  third-party rollout. The general RBAC ADR remains open.

The other eleven carried-forward gaps are unchanged.

## Suggested follow-up ADRs (delta vs. the 2026-04-27 update)

The 2026-04-27 update's suggested-follow-up list remains valid.
Three new candidates observed during this 2026-04-29 update:

- **Bot authorisation model.** Differentiated tool-class filtering
  between bot path and GUI path, plus per-user roles when
  multi-user becomes real. Cross-referenced from ADR-0030
  (*Consequences — Neutral / Follow-ups*).
- **Concurrency model for `ToolRegistry`.** The named fix path for
  the residual race in ADR-0031. Likely bundled with the
  ADR-0018 refactor, but a separate ADR may be warranted at the
  point the registry's lifecycle is reconsidered.
- **UI theme `density` ADR.** The deferred follow-up to
  ADR-0032: `density.preset = "compact" | "comfortable" |
  "spacious"` as a multiplier into `layout.spacing_unit` and
  related slots, written once layout tokens have been migrated and
  proven themselves under the existing variants.

---

## Revision History

| Date       | Author                       | Change                  |
|------------|------------------------------|-------------------------|
| 2026-04-29 | PortfoliFLOW project owner   | Initial second follow-up. Records ADR-0029 / ADR-0030 / ADR-0031 (Accepted) and ADR-0032 (Proposed); the Revision-History rows added to ADR-0017 and ADR-0018; the cross-document edits to `CLAUDE.md` and `docs/architecture.md`; the explicit non-amendment of ADR-0025; and the three handover-named debts plus the planned-but-unimplemented status of ADR-0032. |
