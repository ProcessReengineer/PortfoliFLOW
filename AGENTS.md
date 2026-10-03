<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# AGENTS.md — rules for coding agents

This file is read at the start of a session by coding agents working on
PortfoliFLOW. Claude Code reads it because the repository has no `CLAUDE.md`;
do not add a `CLAUDE.md` or `CLAUDE.local.md` — either would take precedence
and hide this file (ADR-0134).

It holds rules only. Terms are defined in `docs/glossary.md`; use them
precisely and read the glossary before naming anything new. Where documents
disagree, an accepted ADR wins over everything; this file wins on rules;
`docs/glossary.md` is canonical for terms; `docs/architecture.md` is the
narrative.

## What PortfoliFLOW is

An AI-assisted portfolio management platform for institutional investors: a
FastAPI/Jinja2/HTMX web application on PostgreSQL with row-level security per
tenant, organised into nine Areas. It is maintained by one developer with AI
assistance, so every change must be reviewable by a human in one focused
session — no sprawling diffs, no edits outside the stated scope.

| Read | For |
|---|---|
| `docs/architecture.md` | How the system is built and why |
| `docs/glossary.md` | Every project term, its code mapping and definition |
| `docs/adr/README.md` | The decision log and its index; read the ADR before touching its area |
| `docs/ux/ui-standards.md` | The rules every surface is built against |
| `docs/testing.md` | Test tiers, the database, running the suite |
| `docs/roadmap.md` | Planned work |
| `docs/operator-handbook.md`, `db/README.md` | CLI commands, local setup, the database |

## Tree map

| Path | Holds |
|---|---|
| `core/` | Configuration, exceptions, ORM models (`core/models/`), async repositories (`core/repositories/`) |
| `db/` | Alembic configuration and migrations |
| `modules/` | Business modules, one package per Area, registered in `modules/module_registry.py` |
| `services/` | Calculation engines, integrations and orchestration: `analytics/`, `chart_specs/`, `fx/`, `investments/`, `overlay/`, `market_data/`, `irene/`, `watch_desk/`, `transactions/`, `provider_channel/`, `provider_directory/`, `scheduler/`, `tenant_users/`, `credential_vault/`, `web_research/`, `scraper/`, the AI service core and the ToolRegistry, among others |
| `web/` | FastAPI app, routes, templates, static assets, the shell catalogue (`web/shell.py`) |
| `bot/` | The optional Telegram bot |
| `cli/` | The `portfoliflow` console commands |
| `tests/` | The suite; `tests/regression/` holds the architectural guards |

## Dependency rules

```
web/      ──► modules.module_registry, services/, core/ (and bot/ for the in-process bot)
modules/  ──► services/, core/
services/ ──► services/, core/
bot/      ──► services/, core/
cli/      ──► services/, core/ (and web.settings for deployment settings)
core/     ──► nothing inside the project
```

- `core/` imports nothing from within the project.
- `services/` imports only from `core/` and other `services/` packages —
  never from `modules/`, `web/`, `bot/` or `cli/`
  (`test_layer_imports.py`, which pins `core/` and `bot/` as well).
- `modules/` imports from `core/` and `services/`. A module never imports a
  sibling module and never imports from `web/`; shared code goes down into
  `services/`.
- `web/` reaches `modules` only through `modules.module_registry`
  (`tests/regression/test_web_imports_only_module_registry.py`). It never
  imports `matplotlib` (`test_no_matplotlib_in_web.py`), the in-memory
  `DataStore` (`test_web_does_not_import_persistent_data_store.py`) or a
  market-data provider (`test_web_layer_has_no_market_data_provider_imports.py`).
- `bot/` imports only from `core/` and `services/` (ADR-0030).
- `web/` imports `bot/` only to start, stop and report on the optional
  in-process bot (`web/main.py`, `web/routes/provider_credentials.py`).
  `bot/` imports aiogram inside functions, so this works without the `bot`
  extra.
- `services/analytics/` is pure (ADR-0013, ADR-0045): no database session, no
  FastAPI, no PyQt6; data arrives as arguments. It may import DTO dataclasses
  from `core/repositories/` and exceptions from `core.exceptions`
  (`test_analytics_layer_pure.py`). `services/overlay/` and
  `services/market_data/` carry purity guards of their own.
- `services/chart_specs/` consumes `services/analytics/` and emits
  Plotly-shaped dicts; no database access.
- `services/ai_service_core.py` is Qt-free (`test_ai_service_core_qt_free.py`).
  PyQt6 is not imported anywhere (`test_no_qt_imports.py`).
- Imports sit at module level; ruff enforces it (`PLC0415`, tests exempt). A
  function-level import is allowed only for an optional dependency (aiogram,
  `blpapi`, the web app in `tools/ux_inventory.py`) and carries
  `# noqa: PLC0415 - <reason>`.
- Circular imports are a design error. Never break a cycle with a lazy import.
  The one sanctioned exception is the default-tool registration in
  `AIServiceCore._register_default_tools`: the web-research tool imports the
  AI core, so the core loads the tool modules on first construction. Moving
  that registration into the composition root is on the roadmap.

If a task asks you to break one of these rules, stop and say why.

### Mistakes that recur

| Don't | Do |
|---|---|
| Put business logic in a route handler | Put it in `modules/` or `services/`; the route is glue |
| Query the ORM directly | Use the repository in `core/repositories/` |
| Import a concrete module from `web/` | Go through `modules.module_registry`, or move the code into `services/` |
| Read the database from `services/analytics/` | Pass the data in as arguments |
| Instantiate an LLM client | Call `get_ai_service_core()` |
| Add an AI-callable tool beside the ToolRegistry | Register it with an explicit `ToolClass` |

## AI rules

- Never instantiate an LLM client. Route every call through
  `get_ai_service_core()` (`services/ai_service_core.py`).
- Every AI-callable tool is registered through
  `get_tool_registry().register_tool(...)` with an explicit `tool_class`
  (`ToolClass` in `services/tool_classes.py`); the registry rejects a missing
  class (ADR-0012, ADR-0022).
- Content from a `READ_EXTERNAL_UNTRUSTED` tool passes through the Fetcher LLM
  (`send_one_shot_extraction`) and is wrapped in
  `<external_content trust="untrusted">…</external_content>` before it reaches
  an agent's conversation.
- Once a `READ_EXTERNAL_UNTRUSTED` tool has fired in a turn, `WRITE_INTERNAL`
  and `EXTERNAL_EFFECT` tools are locked for the rest of that turn.

## Code conventions

- Python 3.11+ syntax: `list[str]`, `str | None`. Type hints on every public
  API (ADR-0006). Google-style docstrings on every public API (ADR-0007).
- English in code, comments, docstrings, logs, exception messages and
  documentation (ADR-0008). German appears only as data values, such as
  labels in an Excel import file.
- Every new `.py` file starts with the two-line SPDX header:

  ```python
  # SPDX-License-Identifier: AGPL-3.0-only
  # Copyright (c) 2025-2026 Sönke Pinkernelle
  ```

- Exceptions are typed and derive from `PortfoliFlowError`
  (`core/exceptions.py`, ADR-0044).
- No `print()` outside the CLI; log through `logging.getLogger(__name__)`.
- Repositories are async; service methods that perform I/O are async; pure
  calculation in `services/analytics/` is synchronous.
- Module-Scope Rule (ADR-0016): adding a module touches as few existing lines
  as possible. If wiring it needs more than a handful, say so — the seam may
  be wrong.
- An architecturally significant decision gets an ADR before or with the
  implementation (criteria in `docs/adr/README.md`).

## Persistence and migrations

- PostgreSQL with row-level security per tenant (ADR-0034, ADR-0035). Every
  domain table carries `tenant_id`. Reads and writes go through the async
  repositories in `core/repositories/`.
- Migrations are Alembic files in `db/migrations/versions/`, named
  `YYYY_MM_DD_HHMM_bNNN_<descriptor>.py`. Every migration ships a working
  `downgrade()` (ADR-0034 §5); recent migrations carry a roundtrip guard in
  `tests/regression/`.
- A committed migration is not edited; a change is a new migration.
- Never take the current head from a document. Ask the tree:
  `.venv/bin/alembic -c db/alembic.ini heads`. If the task names a revision
  number, confirm it is free before using it.

## UI rules

- `docs/ux/ui-standards.md` governs every surface. Read the rule before you
  build against it.
- Build from the component vocabulary in
  `web/static/css/components/pf_components.css` (`pf-*` classes). Add no new
  legacy or Area-prefixed classes; migrated Areas are pinned by
  `tests/web/test_pf_components.py`.
- Colours, sizes, spacing and durations are `--ui-*` tokens defined in
  `config/ui_theme*.json`. `web/static/css/theme.css` is generated by
  `scripts/generate_theme_artifacts.py` — never edit it by hand. No colour
  literal outside `theme.css` (`tests/web/test_css_tokens.py`).
- Icons come from `pf_icon()` (`web/icons.py`), which raises on an unknown
  name; add the name to `ICONS` rather than inlining an SVG.
- Chart colours come from `config/chart_theme*.json`; they are not UI tokens.

## Adding a module or a view

1. **Confirm the Area.** `module_area` is one of the nine values in
   `VALID_AREAS` (`core/base_module.py`). A new Area needs an ADR. If the Area
   is unclear, ask.
2. **The module.** Create `modules/<area>/<name>.py`: subclass `BaseModule`,
   decorate with `@registry.register` (`modules.module_registry`), set
   `module_name` and `module_area`, implement `run()`. Import it in
   `modules/<area>/__init__.py` — that import is what registers it.
3. **The logic.** Pure calculation goes to `services/analytics/`, integration
   and orchestration to `services/`, persistence through `core/repositories/`.
4. **The route.** A user-facing module gets a router in `web/routes/`,
   included in `web/main.py`. The handler is glue.
5. **The view** (`docs/ux/ui-standards.md` §7): add a `SectionMeta` to the
   Area's tuple in `_SECTIONS_BY_AREA` (`web/shell.py`); add one include of
   `areas/_section.html` to `web/templates/_partials/areas/_<area>_body.html`;
   the body partial is a lazy loader with `hx-trigger="intersect once"`.
   `tests/regression/test_section_catalogue_matches_body_partials.py` pins
   catalogue and partial together.
6. **Tests.** Route tests in `tests/web/test_<module>_routes.py`, service
   tests in `tests/services/`, repository tests in `tests/repositories/`.
7. **Reference example.** `modules/investor_communication/portfolio_review.py`,
   `web/routes/portfolio_review.py`,
   `web/templates/_partials/portfolio_review_section_lazy.html` and
   `portfolio_review_section.html`.

## Tests and toolchain

- The tools live in the virtual environment, not on `PATH`:
  `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `.venv/bin/pyright`, `.venv/bin/pytest`. Ruff's line length is 100.
  Pyright covers only the typing islands listed under `[tool.pyright]` in
  `pyproject.toml`.
- A database-backed test run empties the development database. Afterwards,
  restore it with `portfoliflow bootstrap`.
- Never run two database-backed pytest processes at once — not even two
  single modules. `tests/conftest.py` stops the second run at its first
  database connection; do not work around it.
- A skip is not a pass: a log with `Cannot reach Postgres` proves nothing
  about the database half.
- A test that judges a dated document derives its dates from the document,
  never from today's date.
- Repository and route tests run against the Primary Tenant by default;
  cross-tenant tests use a second tenant explicitly
  (`tests/repositories/conftest.py`, `tests/web/conftest.py`).
- Run the full suite (about two hours) only when the task asks for it.
  Procedure and tiers: `docs/testing.md`.

## Working protocol

1. **Verify first.** Before any write, check the facts the task states —
   paths, anchors, counts, expected outputs — against the tree.
2. **Stop on a mismatch.** If anything differs, write nothing, report the
   discrepancy and end. Never proceed on a discrepancy and never correct the
   task's premise yourself.
3. **One concern per task.** Change only what the task scopes. Anything else
   you notice goes into the report as a finding, not into the diff.
4. **Ask** when the Area, the module, or whether a dependency is acceptable
   is unclear. A wrong assumption costs more to undo than a question.
5. **Reports** go where the task says, never inside the working tree; a
   report on a stopped run likewise. Start with what the operator must do;
   record each check with its command and output; always include a
   "Deliberate deviations" section, saying "None" when there are none.
6. **ADRs are immutable.** A correction is a successor or annex ADR, never an
   edit. Take the next number from `docs/adr/README.md`.
7. **Roadmap items** are not implemented unless the task asks for them.

## Git rules

- Git is **read-only** for agents: `status`, `diff`, `log`, `show`,
  `ls-files` and `blame` only.
- No git write operation of any kind — no `add`, `rm`, `mv`, `commit`,
  `stash`, `restore`, `checkout`, `reset`, `revert`, `cherry-pick`, `merge`,
  `rebase`, `tag` or `push`, and no branch creation or switch. Delete or
  rename files with plain `rm` and `mv`. The operator stages and commits.
- Propose a commit message in Conventional Commits form (ADR-0014),
  imperative mood, without a `Co-Authored-By` trailer.

## Excel import invariants

The Excel import format is specified in ADR-0009; call it the Excel import
format or Excel import file, not "V2" (ADR-0059). Import code must respect:

- Investment columns and their labels are discovered from row 1 of each
  investment sheet; no hard-coded column counts or letter ranges.
- Market reference sheets (such as `interest rates`) have their own column
  namespace and stay out of the investment-column consistency check.
- Empty investment columns are valid placeholder slots; empty plan sheets
  are valid.
- The `Attributes` sheet has a variable number of rows; time-series sheets
  have variable date ranges.
- German labels in the data (`Aktien`, `Typ der Investition`, `Währung`) are
  valid content and stay in the source language.

Detail: ADR-0009 and the docstring of
`services/data_normalization/investment_extractor.py`.

## Legacy

- The PyQt6 desktop surface was removed in July 2026 (ADR-0094 Stage 1); the
  web application is the only surface. The pre-removal state is the
  `demo-stable-pre-qt-sunset` tag.
- The in-memory `core.data_store.DataStore` survives only for the
  DataStore-coupled reporting engine and a few module shells, and is staged
  for removal (ADR-0094 §5, roadmap #035). New code must not depend on it.
