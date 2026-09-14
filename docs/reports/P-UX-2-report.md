# P-UX-2 — Single-source section titles

**Status: completed.** The 27 `section_title="…"` literals are gone from
the nine Area body partials; `web/shell.py`'s `_SECTIONS_BY_AREA` is now
the only place a Section title is declared, and `areas/_section.html`
resolves the rendered `<h2>` from it. All 27 rendered headings are
byte-identical before and after.

Track: UX overhaul, preparation. Decision of record: D-UX-2 (2026-09-13),
an explicit one-off exception to D-UX-A0 "copy over code".
Run date: 2026-09-14.

---

## 1. Operator action required

### 1.1 Commit proposal

```
refactor(web): render section headings from the shell catalogue, retire the 27 template title literals (P-UX-2)
```

Files in the commit (`git status --porcelain`, verbatim):

```
 M docs/ux/inventory/elements.csv
 M docs/ux/inventory/routes.csv
 M docs/ux/inventory/summary.md
 M tests/regression/test_section_catalogue_matches_body_partials.py
 M tests/web/test_section_navigation.py
 M web/main.py
 M web/shell.py
 M web/templates/_partials/areas/_admin_body.html
 M web/templates/_partials/areas/_assistants_body.html
 M web/templates/_partials/areas/_back_office_body.html
 M web/templates/_partials/areas/_cases_body.html
 M web/templates/_partials/areas/_front_office_body.html
 M web/templates/_partials/areas/_investor_communication_body.html
 M web/templates/_partials/areas/_planning_desk_body.html
 M web/templates/_partials/areas/_transactions_body.html
 M web/templates/_partials/areas/_watch_desk_body.html
 M web/templates/areas/_section.html
```

Plus this report (`docs/reports/P-UX-2-report.md`). No other file was
touched. No git operation was performed.

### 1.2 `portfoliflow bootstrap` is due

The web tests truncate `tenants`, `users` and the session/audit tables,
and the after-snapshot walk (§4.1) did the same. The local dev database
is therefore empty of tenants and users. Run:

```
portfoliflow bootstrap
```

before using the dev server again.

### 1.3 Full suite owed at home

Only the named selections were run here (§3.3). The full gate
(~4,930 tests, ≈2h20m) is owed at home.

---

## 2. Verify-first

### 2.1 Check table

| # | Check | Expectation | Observed | Verdict |
|---|-------|-------------|----------|---------|
| 1 | Clean tree, P-UX-0 committed | porcelain empty; tip is the P-UX-0 commit | porcelain empty; tip `2e8ef61 docs: UX overhaul documents updated`, whose six files are exactly the P-UX-0 artefacts (`docs/reports/P-UX-0-report.md`, `docs/ux/README.md`, the three `docs/ux/inventory/*`, `tools/ux_inventory.py`) | **PASS** in substance — the commit *message* does not name P-UX-0. See F-UX-2.1 |
| 2 | Catalogue present | hits at 126 and 231 | `_SECTIONS_BY_AREA` at **126** (definition) and **231** (`all_sections` return) | PASS |
| 3 | Catalogue helpers | `get_area_meta` 211, `all_sections` 221, `section_index_for` 234 | **211 / 221 / 234** | PASS (exact, ±0) |
| 4 | Jinja globals precedent | `Jinja2Templates(` 453, `_pf_area_label` 461, `env.globals` 469 | **453 / 461 / 469** | PASS |
| 5 | Render context | `"active_area": area_slug` at 170; `section_index_for(area_slug)` at 173 | **170 / 173** (plus the import at 57) | PASS |
| 6 | Template carrier | exactly 2 hits: docstring 7, `{{ section_title }}` 23 | **2 hits** at 7 and 23 | PASS |
| 7 | Literals to retire | fo 4, bo 3, as 3, ic 1, pd 2, wd 3, cases 3, tx 3, admin 5; total 27 | **4 / 3 / 3 / 1 / 2 / 3 / 3 / 3 / 5 = 27** | PASS |
| 8 | No other includer | exactly the nine body partials include it; 11 files mention the path | **9 includers, 11 mentions** (the two extras — `_partials/data_import_section.html`, `_partials/market_data_section.html` — mention it in comments only) | PASS |
| 9 | Drift guard | 91 lines, 2 tests | **91 lines, 2 tests** | PASS |
| 10 | Per-area ASGI pattern | line 230, parametrised over `_AREAS` (229) | **230**, `@pytest.mark.parametrize("area_slug,url,section_slugs", _AREAS)` at **229** | PASS — its fixture/parametrisation shape was reused |
| 11 | Titles snapshot (before) | the 27 template pairs equal the 27 catalogue pairs, per area and in order | **27 = 27, zero differences** (table below) | PASS |

Check 11 was re-derived rather than copied from the v2 report, and it
matches: same 27 pairs, zero differences.

### 2.2 The 27 pairs — before

Catalogue order (`_SECTIONS_BY_AREA` insertion order). "Template literal"
is the `section_title="…"` value the body partial carried before this
change; "Catalogue title" is `SectionMeta.title`.

| # | Area | Slug | Template literal | Catalogue title | Match |
|---|------|------|------------------|-----------------|-------|
| 1 | `front_office` | `overview` | Overview | Overview | = |
| 2 | `front_office` | `charts` | Charts | Charts | = |
| 3 | `front_office` | `statistics` | Statistics | Statistics | = |
| 4 | `front_office` | `portfolio-optimizer` | Portfolio Analysis | Portfolio Analysis | = |
| 5 | `watch_desk` | `briefing` | Briefing | Briefing | = |
| 6 | `watch_desk` | `journal` | Journal | Journal | = |
| 7 | `watch_desk` | `calibration` | Calibration | Calibration | = |
| 8 | `planning_desk` | `cash-flow-planning` | Cash Flow Planning | Cash Flow Planning | = |
| 9 | `planning_desk` | `scenario-analysis` | Scenario Analysis | Scenario Analysis | = |
| 10 | `cases` | `open-cases` | Open Cases | Open Cases | = |
| 11 | `cases` | `recently-closed` | Recently Closed | Recently Closed | = |
| 12 | `cases` | `archive` | Archive | Archive | = |
| 13 | `transactions` | `new` | New transaction | New transaction | = |
| 14 | `transactions` | `blotter` | Blotter | Blotter | = |
| 15 | `transactions` | `history` | History | History | = |
| 16 | `back_office` | `saa` | Strategic Asset Allocation | Strategic Asset Allocation | = |
| 17 | `back_office` | `benchmarks-attribution` | Benchmarks & Attribution | Benchmarks & Attribution | = |
| 18 | `back_office` | `limits` | Investment Limits | Investment Limits | = |
| 19 | `admin` | `data-import` | Data Import | Data Import | = |
| 20 | `admin` | `market-data` | Market Data | Market Data | = |
| 21 | `admin` | `providers-credentials` | Providers & Credentials | Providers & Credentials | = |
| 22 | `admin` | `users` | Users | Users | = |
| 23 | `admin` | `investments` | Investments | Investments | = |
| 24 | `investor_communication` | `portfolio-review` | Portfolio Review | Portfolio Review | = |
| 25 | `assistants` | `shirley` | Shirley | Shirley | = |
| 26 | `assistants` | `report-scraper` | Report Scraper | Report Scraper | = |
| 27 | `assistants` | `providers-credentials` | Providers & Credentials | Providers & Credentials | = |

**Differences: 0.**

Note rows 21 and 27: `providers-credentials` is a slug shared by two
Areas with the same title. It is the reason the new helper is keyed on
the `(area, section)` *pair* and not on the slug alone.

---

## 3. Change list

| File | Change |
|---|---|
| `web/shell.py` | `+38 / −1`. New `section_title(area_slug, section_slug) -> str` placed after `all_sections`: a linear scan over `all_sections(area_slug)`, raising `LookupError` for an unknown pair. `SectionMeta`'s docstring gains one sentence naming the catalogue as the single source of the rendered heading. |
| `web/main.py` | `+8 / −0`. `section_title` added to the existing `from web.shell import (…)` block; `templates.env.globals["pf_section_title"] = section_title` registered next to `pf_area_label`, with a four-line comment. No wrapper function. |
| `web/templates/areas/_section.html` | `+5 / −2`. Line 23 → `{{ pf_section_title(active_area, section_slug) }}`. The docstring's `section_title` parameter entry is replaced by a note that the heading comes from the catalogue and cannot be overridden per call site. `section_pill`, `section_body`, `section_body_template` and the placeholder branches are untouched. |
| the nine `_*_body.html` | **27 `section_title=` lines deleted, nothing else** — `git diff --stat` over the directory: `9 files changed, 27 deletions(-)`, 0 insertions. |
| `tests/regression/test_section_catalogue_matches_body_partials.py` | `+71 / −1`. New `test_body_partials_carry_no_title_literals` (regex `section_title\s*=` over all nine partials → zero matches, message naming the offending file and pointing at the catalogue) plus the three helper unit tests (§3.2). Module docstring gains one paragraph; `_SECTION_TITLE_RE` and a `pytest` import added. |
| `tests/web/test_section_navigation.py` | `+53 / −1`. New `test_rendered_section_titles_match_catalogue`, parametrised over the same `_AREAS` as line 229, plus `_SECTION_H2_RE` / `_rendered_section_headings` and the `html` / `re` imports. String-and-regex assertions, matching the neighbours; no parser dependency added. |
| `docs/ux/inventory/*` | Regenerated by `tools/ux_inventory.py` (§4.2). |

### 3.1 Where the helper's unit tests live

In **`tests/regression/test_section_catalogue_matches_body_partials.py`**.
`grep -rln "from web.shell import" tests/` returns four modules; of those,
only this one and `tests/web/test_section_navigation.py` exercise
`all_sections`, and the latter is a live-DB ASGI module that uses
`all_sections` merely to derive its parametrisation. The regression
module is the DB-free unit-test home for the catalogue, and it is where
the no-literals guard belongs anyway, so both landed together.

### 3.2 The three helper unit tests

1. `test_section_title_resolves_a_catalogue_pair` — `section_title("transactions", "blotter") == "Blotter"`.
2. `test_section_title_is_keyed_on_the_pair_not_the_slug` — both
   `("admin", "providers-credentials")` and
   `("assistants", "providers-credentials")` resolve to
   `Providers & Credentials`, **and** `("transactions", "providers-credentials")`
   raises `LookupError`. The third assertion is the one that fails if the
   helper is ever reduced to a slug-only scan.
3. `test_section_title_raises_for_an_unknown_pair` — `("front_office", "no-such-section")` raises `LookupError`.

### 3.3 Test, lint and typecheck results

```
$ python -m pytest tests/regression/test_section_catalogue_matches_body_partials.py \
      tests/web/test_section_navigation.py tests/web/test_transactions_area.py -q
........................................................................ [ 97%]
..                                                                       [100%]
74 passed in 162.06s (0:02:42)
```

Collected counts: **6 + 64 + 4 = 74**. The regression module goes 2 → 6
(the new no-literals guard plus the three helper unit tests); the
section-navigation module goes 55 → 64 (the new test is parametrised over
the nine Areas); the Transactions Area module is unchanged at 4.

`tests/web/test_transactions_area.py` was included because it renders the
Transactions body. `grep -rln "section_title" tests/` returns only the two
modules this prompt edits — **no test anywhere asserted a title literal**
(F-UX-2.4).

```
$ ruff check web/shell.py web/main.py tests/regression/test_section_catalogue_matches_body_partials.py tests/web/test_section_navigation.py
All checks passed!

$ ruff format --check web/shell.py web/main.py tests/regression/test_section_catalogue_matches_body_partials.py tests/web/test_section_navigation.py
4 files already formatted

$ pyright web/shell.py
0 errors, 0 warnings, 0 informations

$ pyright web/main.py
  web/main.py:329:34 - error: Argument of type "str | None" cannot be assigned to parameter "database_url" of type "str" in function "start_bot"
1 error, 0 warnings, 0 informations
```

That single `web/main.py` error is **pre-existing and unrelated** — see
F-UX-2.2.

---

## 4. Definition of done

### 4.1 After-table — rendered headings

Re-derived after the change by logging in over ASGI and issuing a `GET`
for each of the nine Area URLs, then extracting every
`<h2 class="pf-section__title" id="{slug}-title">…</h2>` and collapsing
its text (the same extraction the new web test uses). Listed in sidebar
order, which is the walk order.

| # | Area | Slug | Rendered `<h2>` (after) | Before-literal | Identical |
|---|------|------|-------------------------|----------------|-----------|
| 1 | `front_office` | `overview` | Overview | Overview | ✓ |
| 2 | `front_office` | `charts` | Charts | Charts | ✓ |
| 3 | `front_office` | `statistics` | Statistics | Statistics | ✓ |
| 4 | `front_office` | `portfolio-optimizer` | Portfolio Analysis | Portfolio Analysis | ✓ |
| 5 | `back_office` | `saa` | Strategic Asset Allocation | Strategic Asset Allocation | ✓ |
| 6 | `back_office` | `benchmarks-attribution` | Benchmarks & Attribution | Benchmarks & Attribution | ✓ |
| 7 | `back_office` | `limits` | Investment Limits | Investment Limits | ✓ |
| 8 | `assistants` | `shirley` | Shirley | Shirley | ✓ |
| 9 | `assistants` | `report-scraper` | Report Scraper | Report Scraper | ✓ |
| 10 | `assistants` | `providers-credentials` | Providers & Credentials | Providers & Credentials | ✓ |
| 11 | `planning_desk` | `cash-flow-planning` | Cash Flow Planning | Cash Flow Planning | ✓ |
| 12 | `planning_desk` | `scenario-analysis` | Scenario Analysis | Scenario Analysis | ✓ |
| 13 | `investor_communication` | `portfolio-review` | Portfolio Review | Portfolio Review | ✓ |
| 14 | `watch_desk` | `briefing` | Briefing | Briefing | ✓ |
| 15 | `watch_desk` | `journal` | Journal | Journal | ✓ |
| 16 | `watch_desk` | `calibration` | Calibration | Calibration | ✓ |
| 17 | `cases` | `open-cases` | Open Cases | Open Cases | ✓ |
| 18 | `cases` | `recently-closed` | Recently Closed | Recently Closed | ✓ |
| 19 | `cases` | `archive` | Archive | Archive | ✓ |
| 20 | `transactions` | `new` | New transaction | New transaction | ✓ |
| 21 | `transactions` | `blotter` | Blotter | Blotter | ✓ |
| 22 | `transactions` | `history` | History | History | ✓ |
| 23 | `admin` | `data-import` | Data Import | Data Import | ✓ |
| 24 | `admin` | `market-data` | Market Data | Market Data | ✓ |
| 25 | `admin` | `providers-credentials` | Providers & Credentials | Providers & Credentials | ✓ |
| 26 | `admin` | `users` | Users | Users | ✓ |
| 27 | `admin` | `investments` | Investments | Investments | ✓ |

27 rendered pairs, 27 before-pairs, same key set, **byte-identical on
every one**. The before side was re-read from the *committed* (HEAD)
partials, not from the in-memory table above, so the comparison is
against what actually shipped.

### 4.2 The remaining DoD greps

```
$ grep -rc "section_title=" web/templates/_partials/areas/
…_assistants_body.html:0        …_front_office_body.html:0
…_back_office_body.html:0       …_investor_communication_body.html:0
…_cases_body.html:0             …_admin_body.html:0
…_watch_desk_body.html:0        …_planning_desk_body.html:0
…_transactions_body.html:0
```

Zero in every file.

```
$ grep -rn "pf_section_title" web/
web/shell.py:240:    Registered as the ``pf_section_title`` Jinja global so that
web/main.py:476:    templates.env.globals["pf_section_title"] = section_title
web/templates/areas/_section.html:9:      ``pf_section_title(active_area, section_slug)`` and cannot be
web/templates/areas/_section.html:26:            {{ pf_section_title(active_area, section_slug) }}
```

Four hits, not the two the DoD names. The two *executable* references are
exactly the expected pair (the `main.py` registration and the
`_section.html` call); the other two are prose in docstrings, and one of
them is mandated by §2 of the prompt. See F-UX-2.3.

### 4.3 Inventory re-run

```
$ python tools/ux_inventory.py --template-root web/templates --out docs/ux/inventory
templates: 219  elements: 1181  routes: 168
route pass: dynamic pass via web.main.create_app
```

Element rows **1,208 → 1,181** (−27), exactly as expected. The 27
`jinja_title` rows are gone (that `element_kind` no longer appears in the
inventory at all); the **54 `python_title` rows are unchanged in total**.

```
$ git diff --stat docs/ux/inventory/
 docs/ux/inventory/elements.csv | 125 ++++++++++++++++-------------------------
 docs/ux/inventory/routes.csv   |   2 +-
 docs/ux/inventory/summary.md   |  31 +++++-----
 3 files changed, 65 insertions(+), 93 deletions(-)

$ git diff --numstat docs/ux/inventory/
49      76      docs/ux/inventory/elements.csv
1       1       docs/ux/inventory/routes.csv
15      16      docs/ux/inventory/summary.md
```

`elements.csv` is **49 insertions / 76 deletions**, not the "27 deletions
and 0 insertions" the DoD names. A column-by-column comparison of the two
CSVs accounts for every line, in three groups:

| Group | Rows | Cause |
|---|---:|---|
| Pure deletions | **27** | The intended change: the 27 `jinja_title` rows. |
| Rewritten in place (1 del + 1 ins each) | **18** | The 9 `heading` + 9 `pill` rows sourced from `areas/_section.html` — one pair per Area. Their `visible_text` / `block` columns carry the heading *expression*, which changed from `{{ section_title }}` to `{{ pf_section_title(active_area, section_slug) }}`. Row count, `element_kind` and the `dynamic` flag are unchanged; both spellings were and remain `dynamic`, so they never entered the vocabulary statistics. |
| `source_line` shifted only (1 del + 1 ins each) | **31** | `web/shell.py` ×27 (+3 lines, from the `SectionMeta` docstring sentence §2 itself mandates), `web/main.py` ×1 (+1 import line), `_admin_body.html` ×2 and `_assistants_body.html` ×1 (rows sitting below a deleted line). Every other column identical. |

27 + 18 + 31 = 76 deletions; 18 + 31 = 49 insertions. Nothing moved that
this change did not cause. The DoD's "the 27 `python_title` rows with
`source_file = web/shell.py` are unchanged row for row" holds on every
column **except `source_line`**, which shifted uniformly by +3 — see
F-UX-2.5.

`routes.csv`'s single changed line is the same effect:
`web/main.py,408 → 409` for `_favicon`, below the added import.

`summary.md` is entirely downstream of the −27:

- elements 1208 → 1181; distinct facets 1155 → 1128.
- Per-area element rows drop by exactly that Area's literal count
  (admin −5, assistants −3, back_office −3, cases −3, front_office −4,
  investor_communication −1, planning_desk −2, transactions −3,
  watch_desk −3 = 27). The `headings` column is unchanged everywhere.
- `jinja_title | 27` drops out of the element-kind table.
- Distinct-noun counts fall where a Section title was the only *static*
  occurrence of that noun (admin 12→11, assistants 10→9, back_office
  42→40, cases 6→4, investor_communication 10→9, planning_desk 12→10);
  the repeated-static-text table loses one "Data Import" (22→21) and one
  "Investments" (9→8). See F-UX-2.6 — this matters to P-UX-1.

---

## 5. Findings

**F-UX-2.1 — the P-UX-0 commit message does not name P-UX-0.**
Check 1 expects a tip commit "whose message names P-UX-0"; the tip is
`2e8ef61 docs: UX overhaul documents updated`. Its contents are exactly
and only the six P-UX-0 artefacts, so the *substantive* precondition
(P-UX-0 committed, tree clean) is met and the run continued. Recorded
because a later prompt that greps commit messages for `P-UX-0` will not
find it. No action needed unless the operator wants the message amended.

**F-UX-2.2 — one pre-existing pyright error in `web/main.py`.**
`web/main.py:329` — `start_bot(database_url=resolved_settings.database_url)`
passes `str | None` into a `str` parameter. Confirmed identical on the
HEAD version of the file (same message, same call), so it is not caused
by this change. It surfaces only because §3 step 7 names the file
explicitly: `[tool.pyright] include` is `services/overlay` and
`services/market_data` only, so `web/` is outside the typing island and
CI never type-checks it. Out of scope here; worth a line in whichever
record tracks island expansion.

**F-UX-2.3 — `pf_section_title` appears four times in `web/`, not two.**
Two are executable (the `main.py` registration, the `_section.html`
call) and match the DoD exactly. The other two are documentation: the
`_section.html` docstring note, which §2 of this prompt explicitly
requires, and one sentence in the `section_title` docstring in
`shell.py` naming the global it is registered as. The DoD's "exactly two"
was therefore unreachable as written. Both prose mentions were kept
because they are how a reader finds the seam from either end; strike the
`shell.py` one if the operator prefers the literal count.

**F-UX-2.4 — no test asserted a title literal.**
`grep -rln "section_title" tests/` returns only the two modules this
prompt edits. Nothing in the suite had to be rewritten to accommodate the
deletion. (This confirms the v3 run's finding of the same number.)

**F-UX-2.5 — the "27 deletions, 0 insertions" DoD for `elements.csv`
could not hold, for a reason §2 creates.**
The inventory records a `source_line` per element. §2 mandates a
docstring sentence on `SectionMeta`, which sits *above* `_SECTIONS_BY_AREA`
in `web/shell.py` and therefore shifts all 27 `python_title` line numbers
by +3; the `web/main.py` import shifts one more row and one route row.
Separately, the 18 `heading`/`pill` rows quote the heading expression,
which this change rewrites by design. The net row delta is exactly −27 as
specified; the line-level diff is necessarily larger. Full accounting in
§4.3. No corrective action — but a future prompt writing this DoD should
say "net −27 rows" rather than "27 deletions, 0 insertions", or the
inventory should stop carrying `source_line` in the diffable artefact.

**F-UX-2.6 — the vocabulary statistics lost 27 static occurrences.**
Because the 27 `jinja_title` rows were *static* text, removing them
changes the inventory's distinct-noun and repeated-static-text tables
(details in §4.3). Section titles now appear in the inventory exactly
once each, as `python_title` rows sourced from `web/shell.py` — which is
the point of this change, but it means P-UX-1 will see each Section title
with one occurrence rather than two, and "Data Import"/"Investments"
counts shift accordingly. Nothing is missing from the inventory; the
weighting changed.

**F-UX-2.7 — every deletion was a whole line; no trailing-comma repair
was needed.**
All 27 literals sat as the *middle* assignment of their `{% with %}`
block, followed by `section_body_template=` or `section_body=`. Had any
been last, deleting it would have left a dangling comma before `%}`.
Checked before editing, and re-confirmed by the green template renders.

**F-UX-2.8 — a single render seam carries `active_area`.**
Both the full-page and the HTMX-fragment paths for all nine Areas go
through `_render_area` in `web/routes/areas.py`, which always puts
`"active_area": area_slug` in the context. There is no third path, so the
template's new two-argument lookup cannot be reached with `active_area`
undefined. This is why no route change was needed.

**F-UX-2.9 — the ASGI walk needs `LOCAL_DEV_TENANT_SUBDOMAIN`.**
Outside pytest, the after-snapshot walk returned `404 {"detail":"tenant
not found"}` on `POST /login` until `LOCAL_DEV_TENANT_SUBDOMAIN=minathena-capital`
was set: the `Host` is `testserver`, which carries no subdomain, so the
resolver falls back to the env var. `tests/web/conftest.py:101`
monkeypatches it, which is why the suite is unaffected. Noted for anyone
scripting a render walk outside pytest — including the later UX prompts
that may want one.

---

## 6. Open questions

**OQ-1.** Should the P-UX-0 commit message be amended to name P-UX-0
(F-UX-2.1)? Later prompts in this sequence state preconditions in terms
of commit messages, and the next one (P-UX-0b) may check for this
commit the same way. Amending is the operator's call; this run made no
git operation.

**OQ-2.** Should `docs/ux/inventory/elements.csv` keep `source_line`
(F-UX-2.5)? It makes the CSV a poor diffable artefact — any edit above a
recorded line renumbers unrelated rows, so a reviewer cannot read the
diff as "what changed in the UI". Options: drop the column, move it to a
sidecar, or keep it and state inventory DoDs as row-count deltas. This
is a decision about the tool, so it belongs to P-UX-0b or MC, not here.

**OQ-3.** `section_pill` is still a per-call-site literal in the body
partials, and `_section.html` still accepts `section_body` /
`section_placeholder` HTML inline. F-UX-0.14 already routes the pill
mechanism to ui-standards; should the *placeholder and inline-body*
strings follow it there, or are they in scope for a later P-UX prompt?
Untouched here by §4.

**OQ-4.** Is one sentence of docstring prose naming `pf_section_title` in
`web/shell.py` acceptable, or should the DoD's literal two-hit grep win
(F-UX-2.3)? Trivial to strike; flagged only because the DoD was explicit.
