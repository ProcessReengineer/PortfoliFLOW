# P-UX-0 — UI inventory report

**Track:** UX overhaul, step 0 (successor of the UI polish pass P-UI-1/P-UI-2,
which stays a separate concern).
**Concern:** a complete, machine-readable inventory of every user-visible
surface, element and label in the web UI, plus a route inventory — the data
basis for the vocabulary decision and the per-Area overhaul.
**Repository:** PortfoliFLOW (AGPL), `/home/soenke/Code/PortfoliFLOW/PortfoliFLOW`,
working tree on `main`.
**Run:** 2026-09-13 · no STOP condition fired · all deliverables written ·
read-only analysis, no existing file changed.
**Git:** no operations performed beyond `git status` / `git diff`. The tree
carries the new files; the commit is the operator's.

---

## 1. Operator action required

Five new paths, no existing file touched. `git status --porcelain` at the end
of the run:

```
?? docs/reports/P-UX-0-report.md
?? docs/ux/
?? tools/
```

`docs/ux/` and `tools/` are new directories, so git collapses them. Expanded
(`git status --porcelain --untracked-files=all`):

```
?? docs/reports/P-UX-0-report.md
?? docs/ux/README.md
?? docs/ux/inventory/elements.csv
?? docs/ux/inventory/routes.csv
?? docs/ux/inventory/summary.md
?? tools/ux_inventory.py
```

Nothing else. No tracked file is modified.

| File | State |
|---|---|
| `tools/ux_inventory.py` | new — the re-runnable extraction tool (1,924 lines) |
| `docs/ux/README.md` | new — what the folder is, how to regenerate, generated-artefact warning |
| `docs/ux/inventory/elements.csv` | new — generated, 1,208 element rows |
| `docs/ux/inventory/routes.csv` | new — generated, 168 route rows |
| `docs/ux/inventory/summary.md` | new — generated, the per-Area baseline |
| `docs/reports/P-UX-0-report.md` | new — this report |

Suggested message:

```
chore(ux): add re-runnable UI inventory tool and 2026-09 baseline (P-UX-0)
```

**Decisions the operator must make by hand:**

1. **`tools/` is a new top-level directory.** Check 8 found only `scripts/`;
   `tools/` did not exist and was created per §3 of the prompt. If the project
   would rather keep one directory for executable helpers, the tool moves to
   `scripts/ux_inventory.py` and the one path reference in `docs/ux/README.md`
   changes with it. Nothing else refers to it. My recommendation is to keep
   `tools/` separate: `scripts/` holds deployment and build helpers that the
   application depends on, while this is analysis apparatus that ships with
   the repo but is never called by it.
2. **Whether `tools/ux_inventory.py` joins the lint and typecheck gates.** It is
   `ruff check`-clean, `ruff format`-clean and `pyright`-clean today, but
   `pyproject.toml`'s `[tool.pyright] include` lists only the two typing islands
   (ADR-0110), so nothing keeps it that way. See F-UX-0.12.
3. **Open questions 1–6 in §6**, which shape what P-UX-1 measures.

No stray files to delete. No ADR, roadmap or `CLAUDE.md` change is proposed
here — every candidate for change is a numbered finding in §5.

---

## 2. Verify-first results

Run before anything was written. No STOP condition fired.

| # | Check | Command as run | Result | Mode |
|---|-------|----------------|--------|------|
| 1 | Clean tree, no staged changes | `git status --porcelain` | empty | STOP — **pass** |
| 2 | Template root | `grep -rn "Jinja2Templates(" web/ --include='*.py'` | exactly one hit, `web/main.py:453`; directory argument is `_TEMPLATES_DIR` (`web/main.py:101`) = **`web/templates`** | STOP — **pass** |
| 3 | Area body templates | `find web/templates -name '_*_body.html'` | **9**, all under `_partials/areas/`: `_admin_body.html`, `_assistants_body.html`, `_back_office_body.html`, `_cases_body.html`, `_front_office_body.html`, `_investor_communication_body.html`, `_planning_desk_body.html`, `_transactions_body.html`, `_watch_desk_body.html` | REPORT — ≥ 8 satisfied. The prompt's list of eight omitted Watch Desk; the tree has all nine Areas. |
| 4 | Section-heading partial from P-UI-2 | `find web/templates -path '*areas/_section_heading.html'` | **0 files.** P-UI-2 has not run. The section heading is inline in `web/templates/areas/_section.html` (`<h2 class="pf-section__title">` plus an optional `<span class="pf-section__pill">`). | REPORT — noted, run continued |
| 5 | Route modules | `ls web/routes/*.py \| wc -l` | **26** | REPORT — ≥ 10 satisfied |
| 6 | App factory importable | `python -c "from web.main import create_app; print(len(create_app().routes))"` | **169** | REPORT — dynamic pass available and used |
| 7 | Copy constants in Python | `grep -rn -E "tile=\|label=\|_NEW_SECTION\|MD-1" web/routes/ services/ --include='*.py' \| grep -v 'docs/reports/' \| wc -l` | **173** | REPORT — > 0 satisfied |
| 8 | Tooling directory | `ls -d tools/ scripts/ 2>/dev/null` | only `scripts/` exists; **`tools/` created by this run** | REPORT |
| 9 | `docs/ux/` | `ls -d docs/ux 2>/dev/null` | absent, as expected; created by this run | REPORT |
| 10 | Report folder | `ls docs/reports/` | exists, 9 earlier reports | STOP — **pass** |

**Check 6 deviation.** The prompt's command was `from web.app import app`. There
is no `web/app.py`; `grep -rn "FastAPI(" web/ --include='*.py'` returns a single
hit at `web/main.py:394`, inside `create_app()` — a factory, not a module-level
`app`. The tool tries `web.main:app`, `web.main:create_app`, `web.app:app` in
order and reports which one answered, so it survives either shape.

**Check 4 consequence.** With no `_section_heading.html` to key on, the tool
inventories the section heading by its actual carriers: the `pf-section__title`
`<h2>` (element kind `heading`) and the `{% with section_title=… %}` literal
that feeds it (kind `jinja_title`). The crumb-part extraction described in §2
of the prompt is implemented — any class token containing `crumb` becomes a
`crumb` row — and currently matches nothing, because no crumb markup exists yet.
The tool needs no change when P-UI-2 lands.

**Check 7 note.** 173 is the raw grep count and includes non-copy matches
(`saa_label=`, `xlabel=`, `label="tangency_portfolio[QP]"`). The tool's Python
pass is narrower and typed (§3), and lands 108 rows of genuine user-visible copy.

---

## 3. Method

### 3.1 Template pass — as executed

219 templates under `web/templates`, read in full. For each file:

1. **Scrub.** `{# … #}` comments and `{% … %}` tags are blanked to spaces with
   every newline preserved, so `HTMLParser.getpos()` line numbers are the line
   numbers of the original file — verified exactly against three known
   elements in §4.5. `{{ … }}` expressions are kept as literal text with `"`,
   `<` and `>` entity-escaped, which `convert_charrefs` undoes again in text
   nodes and attribute values. The result is that a dynamic label is recorded
   verbatim (`{{ ticket.status }}`) and flagged `dynamic`, exactly as specified,
   and an expression inside an attribute cannot break the parse.
2. **Jinja structure.** `{% block %}` ranges (for the `block` column), literal
   `{% include %}` / `{% extends %}`, and `{% with %}` / `{% set %}`
   string-literal assignments. The last matters here: this codebase composes
   Sections through `{% with section_slug=…, section_title=…,
   section_body_template=… %}{% include "areas/_section.html" %}`, so the
   section titles and the include edges both live in `with` blocks rather than
   in markup. Three `section_body='…'` assignments hold inline HTML; those are
   parsed as fragments and their rows carry the `inline-html` flag.
3. **Elements.** One row per (element, facet) — an element that is both a
   button and an HTMX trigger produces two rows, which is what makes the
   per-Area "buttons" and "HTMX triggers" counts in `summary.md` mean anything.
   1,208 rows over 1,155 distinct facets.

### 3.2 Route pass — as executed

**The dynamic pass ran.** `web.main.create_app()` imported cleanly and yielded
168 inventoried routes (169 `app.routes` entries; the `/static` mount has no
endpoint). Each row carries `methods`, `path`, `endpoint.__module__`,
`endpoint.__name__`, `co_filename:co_firstlineno`, the rendered template(s), and
the authentication dependency, taken by walking `route.dependant` recursively
for `require_super_admin` / `require_session` / `get_authenticated_user`.

The static fallback was exercised as a check (`--no-import`) and works: 145
routes, and an **identical 1,208-row element inventory** — Area attribution does
not depend on which route pass ran. The fallback misses FastAPI's own
`/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect` and the handful of
routes registered outside a `@router.<verb>` decorator.

**Template resolution.** Naïvely reading the first string argument of
`TemplateResponse` finds almost nothing in this codebase: handlers pass a
module-level constant (`planning_desk.py`'s `_SECTION_TEMPLATE`), or call a
local helper that prepends a directory (`transactions.py`'s `_render`, which
builds `f"_partials/transactions/{template}"`), or index a lookup table
(`cases.py`'s composer dict, `transactions.py`'s `_FLOWS`). The tool therefore
indexes each handler module for module-level string constants, f-string
prefixes used in `TemplateResponse`, and the local call graph, then resolves a
handler's templates three levels deep, with the module-level string pool as a
last resort when nothing else resolves. That is the difference between 49
templates stranded in `unassigned` and **zero**.

### 3.3 Area attribution — how the tool decides

Areas are derived, not hand-listed. Seeds come from template paths alone
(`_partials/areas/_<area>_body.html` and `areas/<area>.html`, plus `base.html` /
`_auth_base.html` / `_partials/area_fragment.html` pinned to the synthetic
`_shell`, plus any other top-level template directory as its own surface). They
then propagate to a fixed point over three edge kinds: `template --include-->
template`, `template --hx/form-action--> route`, `route --renders--> template`.
Three rules keep it honest, each of which cost a wrong answer to find:

* **`{% extends %}` is never traversed.** Layout inheritance is shell chrome,
  not Area composition; traversing it counted `base.html` nine times over.
* **`_shell` is a sink.** Its chrome partials reach it by `{% include %}`, but
  its links into the nine Areas propagate nothing — otherwise the sidebar, which
  links to every Area, would own all of them.
* **A plain `href` never propagates into another whole page** (a template that
  transitively extends `base.html`). Linking to a page is navigation;
  only a link that pulls in a fragment is composition. Without this, the
  `/investments/*` CRUD surface was absorbed into four Areas at once.

Two post-passes handle routes no template triggers visibly: a route reachable
only from the shell chrome becomes `_shell`, and a route whose sibling routes in
the same handler module all agree on one Area inherits it. The 29 routes that
needed the second are listed by name in `summary.md`; they are exactly the
routes whose caller is JavaScript (F-UX-0.2).

### 3.4 Python copy pass

Every `.py` under `web/`, plus any module anywhere in the project whose name
contains `copy`, `label`, `vocabulary` or `constants`. Rows come from AST:
keyword arguments and dict entries whose key is one of 17 copy names
(`label`, `title`, `heading`, `crumb`, `tile`, `subtitle`, `pill`, `caption`,
`placeholder`, `kicker`, `hint`, `summary`, `message`, `headline`, `blurb`,
`description`) and whose value is a string constant, plus module-level prose
constants in the name-matched modules. 108 rows, `source_kind = python`.
Python rows inherit the Area of their module's routes; `web/**.py` with no
attributed route falls back to `_shell`.

The three name-matched modules — `core/tenant_constants.py`,
`services/transactions/constants.py`, `services/investments/ta_profile_constants.py`
— yielded **zero** user-visible rows. `services/transactions/constants.py` says
why in its own docstring: "No user-facing copy lives in this module." There is
no central copy module in this project (F-UX-0.9).

### 3.5 Parse errors and warnings

**Zero parse errors and zero structural warnings across all 219 templates.**
Both mechanisms were verified against synthetic input (`<div><p>hi</div></span>`
→ `unbalanced </span>`; `<section><h2>Open</h2>` → `unclosed <section>`), so the
zeros are a result, not a silent failure. Every partial in this tree closes
what it opens.

One consequence of the blanking strategy is worth knowing: a conditional
attribute value such as `href="{% if x %}/a{% else %}/b{% endif %}"` yields both
branches in the target, space-separated (`/a /b`). It happens once, at
`web/templates/investments/_form.html:85`. Targets are whitespace-collapsed, so
line-wrapped URLs — five of them in this tree — read cleanly.

### 3.6 Exclusions

`<script>`, `<style>`, HTML and Jinja comments, and `<select>` option texts are
not inventoried (the option count per select rides on the row as an `options:N`
flag: 28 selects with 1 option, 14 with 2, 2 with 4). No `/dev/` templates exist.
Two templates are excluded as reachable only without login, computed from the
routes' own auth dependencies rather than by name: **`login.html`** and
**`_auth_base.html`**. There is no marketing or homepage template — `GET /`
is a redirect (F-UX-0.3).

---

## 4. Baseline numbers

The "before" the track will be measured against. Copied from
`docs/ux/inventory/summary.md`, which is generated.

### 4.1 Totals

| Measure | Value |
|---|---:|
| Element rows | **1,208** |
| Distinct element facets (file, line, kind, text) | 1,155 |
| Routes | **168** |
| Templates read | 219 |
| Templates excluded (no-login surfaces) | 2 |
| Templates with no owning Area | **0** |
| Parse errors | 0 |
| Structural warnings | 0 |
| Duplicate-label groups | 22 |
| Synonym-candidate groups | 25 |

### 4.2 Per Area

| Area | templates | element rows | headings | buttons | HTMX triggers | links | form labels | pills | tiles | routes | distinct nouns |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `_shell` | 4 | 56 | 0 | 2 | 1 | 3 | 0 | 0 | 0 | 2 | 21 |
| admin | 13 | 119 | 20 | 17 | 22 | 5 | 12 | 10 | 8 | 21 | 12 |
| assistants | 13 | 88 | 5 | 24 | 16 | 5 | 6 | 3 | 0 | 17 | 10 |
| back_office | 19 | 111 | 20 | 15 | 9 | 8 | 15 | 2 | 7 | 18 | 42 |
| cases | 17 | 84 | 8 | 17 | 20 | 8 | 9 | 5 | 0 | 13 | 6 |
| front_office | 17 | 104 | 13 | 5 | 11 | 12 | 6 | 4 | 9 | 11 | 14 |
| `investments` | 11 | 104 | 16 | 28 | 0 | 8 | 32 | 3 | 0 | 22 | 18 |
| investor_communication | 6 | 49 | 7 | 2 | 4 | 3 | 2 | 1 | 10 | 3 | 10 |
| planning_desk | 14 | 110 | 10 | 18 | 20 | 5 | 13 | 3 | 1 | 5 | 12 |
| `super_admin` | 8 | 38 | 6 | 6 | 0 | 2 | 6 | 5 | 0 | 10 | 11 |
| transactions | 31 | 214 | 4 | 46 | 49 | 2 | 69 | 7 | 0 | 23 | 15 |
| watch_desk | 22 | 131 | 9 | 20 | 20 | 8 | 18 | 3 | 5 | 18 | 10 |
| `unassigned` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 0 |

Three of the thirteen rows are not Areas: `_shell` is the chrome, and
`investments` and `super_admin` are full-page surfaces outside the nine-Area
IA (F-UX-0.1). `areas/_section.html` is shared by all nine Areas and is
therefore counted once per Area, per the prompt's rule; it contributes one
heading and one pill to each.

### 4.3 Element rows by kind

| kind | rows | | kind | rows |
|---|---:|---|---|---:|
| button | 200 | | tile | 40 |
| form_label | 188 | | `jinja_title` | 27 |
| htmx | 172 | | area_header | 9 |
| heading | 118 | | `python_hint` | 7 |
| tooltip | 82 | | `python_message` | 3 |
| link | 69 | | `python_description` | 1 |
| empty_state | 61 | | | |
| `python_title` | 54 | | **template** | 1,073 |
| pill | 46 | | **python** | 108 |
| placeholder | 44 | | **jinja** | 27 |
| select | 44 | | | |
| `python_label` | 43 | | | |

HTMX verbs: GET ×109, POST ×63. No `hx-put`, `hx-patch` or `hx-delete` anywhere
in the tree.

### 4.4 Flag totals

| flag | rows | reading |
|---|---:|---|
| `dynamic` | 280 | label is a Jinja expression, recorded verbatim, not resolved |
| `duplicate-label` | 127 | rows in the 22 duplicate groups |
| `synonym-candidate` | 123 | rows in the 25 synonym groups |
| `from:aria-label` | 64 | tooltip rows sourced from `aria-label` |
| `abbrev` | 61 | |
| `icon-only` | 33 | 32 are `tooltip` rows on tables and inputs; one real icon control |
| `internal-name` | 29 | |
| `from:title` | 18 | tooltip rows sourced from `title` |
| `identifier-leak-loose` | 14 | literal kebab-case heuristic; all 14 are ordinary hyphenated English |
| `hidden` | 9 | `hidden` polling elements, excluded from `icon-only` |
| `inline-html` | 3 | rows lifted out of a `section_body='…'` literal |
| `identifier-leak` | **1** | the refined heuristic; one genuine leak |
| `no-accessible-name` | **0** | |

### 4.5 Sanity spot-check

Three elements read from the template and Python source, matched against their
CSV rows. Line numbers verified against the files.

**A button** — the MD-1 flow chooser's first tile, `web/templates/_partials/transactions/_chooser.html:23`:

```
transactions,_partials/transactions/_chooser.html,,template,button,Buy or sell units An instrument
already on the book. U-BUY / U-SELL,/api/transactions/order-form,#tx-composer-host,
web/templates/_partials/transactions/_chooser.html,23,abbrev|duplicate-label
```

The visible text is the button's full subtree — name, hint and flow code, which
is how a user reads the tile. `abbrev` fires on `U-BUY`. The same element also
produces an `htmx` row with `verb:GET`.

**An HTMX trigger** — the Overview lazy shell, `web/templates/_partials/overview_section_lazy.html:8`:

```
front_office,_partials/overview_section_lazy.html,,template,htmx,Loading overview…,
/api/overview/section,this,web/templates/_partials/overview_section_lazy.html,8,verb:GET
```

This is the edge that carries `front_office` onto `/api/overview/section` and
from there onto the two partials that route renders
(`_partials/overview_section.html` and `_partials/overview_error.html`) and
everything they include.

**A Python-defined tile label** — a KPI strip label, `web/routes/charts.py:247`:

```
front_office,,charts,python,python_label,TVPI,,,web/routes/charts.py,247,abbrev
```

`block` carries the module stem for Python rows, and the Area comes from the
routes that module serves.

---

## 5. Findings

Numbered `F-UX-0.n`. Each is a proposal, not a change: nothing in the tree was
modified.

**F-UX-0.1 — Two full-page surfaces live outside the nine-Area IA.**
`/investments/*` (11 templates, 104 element rows, 22 routes) and `/super-admin/*`
(8 templates, 38 rows, 10 routes) are whole pages reached by link, not Sections
inside an Area's long-scroll page; together they are 12 % of the element
inventory and the Admin Area's "Investments" Section is a placeholder sentence
plus a link out to one of them
(`web/templates/_partials/areas/_admin_body.html:57-61`).
→ `elements.csv`, `area` in (`investments`, `super_admin`).

**F-UX-0.2 — Twenty-nine routes have no visible trigger in any template.**
Their caller is JavaScript (`web/static/js/saa_section.js`, the inline scripts in
`investments/detail.html` and `investments/edit.html`, `chat.js`, `scraper.js`),
so an HTML-only inventory cannot see what invokes them; they are attributed by
handler-module agreement, which is the weakest evidence in the inventory. Every
SAA mutation and every `/investments/*` mutation is in this set.
→ `summary.md` § "Routes attributed by handler module".

**F-UX-0.3 — Nine routes cannot be attributed at all.** `GET /api/cmd-search`
(the command palette's backend — arguably `_shell`, but nothing in a template
names it), `GET|POST /login`, `GET /health`, `GET /favicon.ico`, and FastAPI's
own `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`. Note also that
`GET /` — the root redirect — lives in `web/routes/chat.py` and is therefore
attributed to `assistants`; that is a real oddity of module ownership, not a
tool artefact.
→ `summary.md` § "Unassigned routes".

**F-UX-0.4 — Every Section title is declared twice, and today they agree.**
27 `jinja_title` rows in the Area body templates and 27 `python_title` rows in
`web/shell.py`'s section index. The sets are identical — zero drift — but
nothing enforces that, and a rename in the overhaul must touch both files or the
command palette and the section indicator will disagree with the heading they
point at. Making one the source of the other is a cheap change with a real
payoff for this track.
→ `elements.csv`, `element_kind` in (`jinja_title`, `python_title`).

**F-UX-0.5 — Twenty of the twenty-two "Data Import" mentions are deep links out
of an empty or error state.** Fourteen different partials — Overview, Charts,
Statistics, Portfolio Analysis, Limits, Benchmarks & Attribution (three of
them), Portfolio Review, Planning Desk, and the error partial of most of those
— end with a link to `/admin#data-import`, across five Areas (Front Office,
Back Office, Investor Communication, Planning Desk, and Admin itself, which
shares the Overview partials). One Admin Section is the recovery path for most
of the product's empty states. Whether that is right is an IA question the track
should answer deliberately.
→ `elements.csv`, `target` = `/admin#data-import`.

**F-UX-0.6 — Twenty-five synonym groups: different words, same destination.**
The sharpest are `/investments` reached by six different labels ("All
investments", "Apply filters", "Change investment settings", "Investments",
"Reset", "← Back to investments"); `/api/transactions/draft` by "Continue",
"Keep as draft" and "Save as draft"; `/api/transactions/chooser` by "Close",
"Close Discard" and "New transaction"; and `{{ pin_url }}?cancel=1` by "Cancel",
"Close" and "Dismiss". The Cancel/Close/Dismiss triple recurs across Cases,
Planning Desk and Assistants and is the single highest-value vocabulary decision
in the list.
→ `summary.md` § "Synonym candidates".

**F-UX-0.7 — Twenty-two duplicate-label groups, most of them form fields
repeated across the five Transactions composers.** "Linked case (optional)",
"Note (optional)", "Source (optional)", "Trade date", "Settlement date
(optional)", "Fees (optional)", "Taxes (optional)", "Vintage year" each appear
in two to four composers with different field ids. That is duplication in the
markup, not necessarily in the vocabulary — but it means a wording change has
four edit sites, and it is the strongest argument in the inventory for a shared
field partial.
→ `summary.md` § "Duplicate labels".

**F-UX-0.8 — Sixty-one rows carry an abbreviation; twenty-five distinct tokens.**
`NAV` ×18 and `SAA` ×8 dominate, then the Transactions flow codes (`U-BUY`,
`U-SELL`, `R-SEC-BUY`, `R-SEC-SELL`, `R-COMMIT`, `U-NEW`), then the metric
alphabet `TVPI` `DPI` `IRR` `TWR` `IR` `TE` `YTM` `OAS` `AUM`, then `AnlV`,
`GP`, `PDF`, `AGPL`. Three are not product vocabulary at all and read as leaks:
`UUID` in a validation message (`web/routes/benchmarks_attribution.py:452`,
"Selected investment id is not a valid UUID."), `SHA` in the status bar, and
`ADR-0061` in an empty state (`_partials/benchmarks_attribution_stage_a.html:37`,
"see ADR-0061 for the expected Excel format") — a user-facing pointer at an
internal decision record. A fourth, `_partials/saa_empty_state.html:4`, tells the
user to "run `portfoliflow bootstrap`", a CLI command, from a web empty state.
→ `elements.csv`, `flags` contains `abbrev`.

**F-UX-0.9 — There is no central copy module.** The three modules matching
`copy|label|vocabulary|constants` contain no user-facing strings by design;
copy lives in 219 templates and inline in 8 route modules (108 rows, led by
`web/shell.py` with 36 and `web/routes/charts.py` and `web/routes/watch_desk.py`
with 16 each). This is not a defect — it is the fact the vocabulary decision has
to plan around, because a vocabulary change is a distributed edit today.
→ `elements.csv`, `source_kind` = `python`.

**F-UX-0.10 — The identifier-leak heuristic as specified is almost pure noise;
the refined one finds exactly one leak.** The literal P-UX-0 rule (kebab-case
anywhere) flags 14 rows, all ordinary hyphenated English: "read-only",
"as-of date", "month-end", "investment-vs-benchmark", "super-admins",
"lower-priority", "non-cash", "drill-down", "tenant-wide". The tool therefore
emits two flags: `identifier-leak` for the high-confidence reading (snake_case
anywhere, a `pf-` prefix, a `_`-led name, or a label that *is* nothing but a
kebab-case identifier) and `identifier-leak-loose` for the literal rule, so the
noise stays separable. The single genuine leak is a form placeholder reading
`e.g. global_equity` (`web/templates/_partials/saa_asset_classes_modal.html:32`).
**This is a deliberate deviation from §3.3 of the prompt, flagged here for the
operator to accept or reverse.**

**F-UX-0.11 — Twenty-nine rows carry internal vocabulary, but most of them are
canonical product terms.** "tenant" ×8, "Shirley" ×6, "Watch Desk" ×4, "Impact"
×4, "Blotter" ×3, "reported" ×2, "ADR-" ×1, "FIGI" ×1. `CLAUDE.md`'s glossary
makes Watch Desk, Blotter and Shirley canonical, so flagging them is the
heuristic doing what it was told rather than finding a defect — which is itself
the question the vocabulary decision has to settle (see OQ-2). "tenant" is the
interesting one: six of its eight are in Super-Admin and Admin, where the
audience is an operator and the word is the right one, but two are not — a
Front-Office empty state reading "…across the active investments in this
tenant" (`_partials/portfolio_analysis_chart_partial.html:72`) and an Admin
message reading "tenant-wide provider settings are owner-managed"
(`web/routes/provider_credentials.py:985`). The starting list was extended with
eight siblings observed in this tree — `sentinel`, `repace`, `adr-`, `htmx`,
`datastore`, `stammtabelle`, `upsert`, `sidecar` — of which only `adr-` matched.
→ `elements.csv`, `flags` contains `internal-name`.

**F-UX-0.12 — Icon-only controls are all accessibly named; `<select>` usage is
trivial.** After excluding `hidden` pollers (9) and form controls named by their
`<label>`, exactly one element in the tree is an icon-only control
(`_partials/shirley_section.html:54`, the conversation-history button) and it
carries an `aria-label`. Zero rows have `no-accessible-name`. Separately, 44
`<select>` elements exist but 28 have a single option and 14 have two — worth
knowing before the overhaul reaches for a richer control.
→ `elements.csv`, `flags` contains `icon-only` / `options:`.

**F-UX-0.13 — `tools/ux_inventory.py` is outside the lint and typecheck gates.**
It is `ruff check`-, `ruff format`- and `pyright`-clean today (§7), but
`[tool.pyright] include` in `pyproject.toml` lists only `services/overlay` and
`services/market_data` (ADR-0110), so pyright will not see it again unless it is
named explicitly. Since the tool is the track's before/after instrument and will
be edited repeatedly, adding `tools/` to the pyright island set is worth
considering — as a one-line `pyproject.toml` change the operator makes, not one
this prompt is allowed to make.

**F-UX-0.14 — The section-pill mechanism exists but has no user.**
`web/templates/areas/_section.html` renders a `pf-section__pill` when
`section_pill` is set; no Area body sets it, consistent with the most recent
commit ("removed outdated pills"). The nine `pill` rows attributed to
`areas/_section.html` are that dormant markup, counted once per Area. Either the
mechanism gets a use in the overhaul or it goes.

**F-UX-0.15 — Two surprises worth recording.** First, the `pf-area__header`
element wraps both the Area title and its subtitle, so an `area_header` row's
visible text is the two concatenated ("Watch Desk Calm-by-default briefing — what
changed materially since you last looked"); that is the specified rule working
as written, and it happens to be a useful read of what each Area promises — all
nine in one place. Second, Transactions has **4 headings against 69 form labels
and 49 HTMX triggers**, an outlier shape: the newest Area is almost entirely
composer forms with no internal heading structure, where Back Office is 20
headings against 15 form labels. That contrast is the clearest per-Area signal
in the baseline.

---

## 6. Open questions

**OQ-1 — Does `tools/` stay, or does the tool move to `scripts/`?**
(yes = keep `tools/`) — see §1.1. Recommendation: keep it.

**OQ-2 — Are the glossary Area names in scope for the vocabulary decision?**
The `internal-name` heuristic was handed "Watch Desk", "Shirley" and "Blotter"
as internal vocabulary, but `CLAUDE.md`'s glossary makes all three canonical
product terms, and two of them are Area labels in the sidebar. Is the overhaul
allowed to rename a glossary term (which means an ADR), or is the glossary
fixed and the overhaul works below it? (yes = the glossary is in scope)

**OQ-3 — Should the refined `identifier-leak` split in F-UX-0.10 stand?**
(yes = keep both flags, with `identifier-leak-loose` carrying the literal rule)

**OQ-4 — Should P-UX-1 add a JavaScript pass?** F-UX-0.2 shows 29 routes whose
trigger is invisible to an HTML-only inventory, and `web/static/js/` holds the
labels those interactions render. Screenshots will catch the rendered result but
not the source location. (yes = P-UX-1 inventories `web/static/js/` too)

**OQ-5 — Do `investments` and `super_admin` get overhauled as Areas?** They are
12 % of the element inventory and belong to no Area (F-UX-0.1). Either they are
in the per-Area overhaul as two extra surfaces, or the track states they are out
of scope. (yes = they are in scope as two additional surfaces)

**OQ-6 — Is the four-item copy cleanup in F-UX-0.8 in scope for P-UX-0's
successor, or is it a separate small fix now?** The `ADR-0061` reference, the
`portfoliflow bootstrap` instruction, the `UUID` validation message and the
`SHA` label are four one-line edits that need no vocabulary decision.
(yes = fold them into the first Area's overhaul rather than fixing them now)

---

## 7. Verification of the deliverables

Exact commands and their final lines, run from the repository root with the
virtualenv active.

```
$ source .venv/bin/activate && ruff check tools/ux_inventory.py
All checks passed!

$ source .venv/bin/activate && ruff format --check tools/ux_inventory.py
1 file already formatted

$ source .venv/bin/activate && pyright tools/ux_inventory.py
0 errors, 0 warnings, 0 informations
```

`ruff format --check` is included because `ruff format` is the project's
formatter and a CI gate (ADR-0109 §2). The pyright invocation names the file
explicitly, which overrides `[tool.pyright] include` — see F-UX-0.13.

The single run that produced all three inventory artefacts:

```
$ source .venv/bin/activate && python tools/ux_inventory.py \
      --template-root web/templates --out docs/ux/inventory
templates: 219  elements: 1208  routes: 168
route pass: dynamic pass via web.main.create_app
written to: /home/soenke/Code/PortfoliFLOW/PortfoliFLOW/docs/ux/inventory
$ echo $?
0
```

The static fallback was exercised separately into a scratch directory
(`python tools/ux_inventory.py --no-import --out …`), exit 0, 145 routes, and an
element inventory byte-identical in row count to the dynamic run.

**Determinism.** Two runs in separate processes into two scratch directories
produce byte-identical outputs (`diff -r`), and re-running into
`docs/ux/inventory` changes nothing. This matters more than it sounds: the tool
is the before/after instrument for the whole track, so its output has to be
diffable. The first attempt was *not* deterministic — the template-resolution
walk iterated a `set` of helper-function names, and Python's per-process string
hash seed reordered the `template` column of six routes between runs. Fixed by
sorting the walk frontier.

---

## 8. Files touched

Five new paths, plus this report. No existing file modified, renamed or
reformatted.

| Path | Lines |
|---|---:|
| `tools/ux_inventory.py` | 1,924 |
| `docs/ux/README.md` | 30 |
| `docs/ux/inventory/elements.csv` | 1,209 (header + 1,208 rows) |
| `docs/ux/inventory/routes.csv` | 169 (header + 168 rows) |
| `docs/ux/inventory/summary.md` | 243 |
| `docs/reports/P-UX-0-report.md` | 608 |

Out of scope and not done, as specified: no renaming, no template or CSS edit,
no ADR, no roadmap or `CLAUDE.md` change, and no screenshot atlas — that is
P-UX-1, needs a running instance and a bootstrapped tenant, and was not started.
