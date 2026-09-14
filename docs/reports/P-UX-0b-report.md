# P-UX-0b — JavaScript pass for the UI inventory

Extends `tools/ux_inventory.py` with a JavaScript pass, answering P-UX-0
finding F-UX-0.2 / OQ-4. Of the 29 routes the inventory could previously
attribute only by handler module, **23 now have a script caller found and 14 of
those a linked trigger element**; 6 remain unexplained and are listed verbatim
below. Existing template and Python rows come out byte-identical.

---

## 1. Operator action required

### Commit proposal

Nothing is committed by this prompt. Staged nothing; the working tree carries
the four changed files plus this report.

```
chore(ux): inventory JavaScript callers and link them to their trigger elements (P-UX-0b)
```

### Should `pyproject.toml`'s pyright include gain `tools`?

**Not as a one-line edit, and not without an ADR addendum.** The current island
set is:

```toml
[tool.pyright]
include = [
    "services/overlay",
    "services/market_data",
]
```

**Does ADR-0110 enumerate the islands by name? Yes — that is exactly what it is
for.** `docs/adr/0110-typing-island-set-analytics-deferred.md` supersedes
ADR-0109 §3 "island set only", records the three islands ADR-0109 §3 named
(`services/analytics/`, `services/overlay/`, `services/market_data/`), and
decides at §1 that `include` covers `services/overlay/` and
`services/market_data/` **only**, with `services/analytics/` deferred behind
`pandas-stubs` adoption (tracked as the first entry of #054's post-release
typing note).

So the set is a decided, enumerated membership, and every member is shipping
service code. `tools/` is a developer utility that ships to nobody. Adding it
both contradicts the enumeration and widens the set's rationale from "the code
we ship" to "code we happen to type-check", which is a different decision.
**Recommendation: an ADR-0110 addendum that either admits non-shipping tooling
as a distinct island class or declines it, before `pyproject.toml` is touched.**
F-UX-0.13 identified the mechanical fix correctly but appears to have
under-weighted that the list is ADR-governed rather than incidental.

Nothing is blocked meanwhile: prompt rule §0.4 already has the tool
type-checked by naming the file, which needs no configuration at all —
`pyright tools/ux_inventory.py` is clean (§7).

### Two deliberate deviations from §2.5's flag list, for acceptance or rejection

The design enumerates the row flags. Two more are emitted, both provenance
rather than classification, and both trivially removable:

* `via:<wrapper>` — the row was resolved at the call site of a local wrapper
  rather than at a literal call. Without it a reader cannot tell a directly
  observed endpoint from a derived one. **This one carries the pass**: without
  wrapper resolution, `saa_section.js` contributes one unresolvable row instead
  of the seven endpoints it actually calls (F-UX-0b.2).
* `url-from-attribute` — the URL was recovered by following a `data-*` read to
  the template attribute that writes it (3 rows).

---

## 2. Verify-first

Run against the tree at `639eded` (the P-UX-2 commit), clean.

| # | Check | Expectation | Actual | Mode | Result |
|---|---|---|---|---|---|
| 1 | Clean tree | empty | empty | STOP | pass |
| 2 | Tool present | 1,924 or larger | **1,924** | REPORT | pass — the P-UX-0 file, untouched by P-UX-2 |
| 3 | Baseline row count | 1,208 or 1,181 | **1,181** | STOP | pass — post-P-UX-2 |
| 4 | Static JS files | six, given line counts | all six, **counts exact** (114/557/94/910/221/324) | STOP | pass |
| 5 | Inline scripts | detail 12 `fetch(`, edit 1 | **12 and 1**; 28 templates carry `<script>` | REPORT | pass |
| 6 | Call surface | saa≥6, chat≥14, scraper≥8, nav≥3, import≥2, snapshot 0 | **5, 3, 2, 3, 2, 0** | REPORT | **three shortfalls — F-UX-0b.1** |
| 7 | The 29 | 29 `* \`` lines | **29** | STOP | pass |
| 8 | Determinism guard | hits | hits at 228, 956–957, 964, 1023, 1058, 1108, 1113, 1252, 1415 | REPORT | pass |
| 9 | Tool entry points | 733/1121/1295/1528/1546/1776/1805 | **exact, ±0** | REPORT | pass |

The full list for check 5 (28 templates containing `<script`):
`_partials/areas/_planning_desk_body.html`,
`_partials/benchmarks_attribution_section.html`,
`_partials/benchmarks_attribution_stage_a.html`,
`_partials/cases_detail_timeline.html`,
`_partials/charts_investment_triplet.html`, `_partials/chat_history.html`,
`_partials/data_import_section.html`, `_partials/limits_section.html`,
`_partials/overview_section.html`,
`_partials/planning_desk_cash_flow_planning.html`,
`_partials/planning_desk_hyp_form.html`,
`_partials/planning_desk_scenario_results.html`,
`_partials/portfolio_analysis_chart_partial.html`,
`_partials/portfolio_analysis_section.html`,
`_partials/portfolio_review_investment_section.html`,
`_partials/portfolio_review_section.html`,
`_partials/saa_asset_classes_modal.html`,
`_partials/saa_configuration_partial.html`,
`_partials/saa_optimization_partial.html`, `_partials/saa_section.html`,
`_partials/statistics_section.html`,
`_partials/watch_desk_calibration_editor.html`, `base.html`,
`investments/_nav_chart_partial.html`, `investments/charts.html`,
`investments/detail.html`, `investments/edit.html`, `investments/list.html`.

An earlier attempt at this prompt stopped at check 1, when the P-UX-2 delivery
was still uncommitted in the working tree. That STOP report was written outside
the repo and no file was written into the tree, so it cannot affect this run.

---

## 3. Method

### The comment stripper

A character state machine (`strip_js_comments`, ~70 lines with the docstring)
over five states: code, line comment, block comment, string (single, double,
template) and regular-expression literal. It is not a regex, because `//`
inside a string literal is not a comment. Regex literals are tracked because a
`/` after a value is division, not a literal — the state is entered only when
the `/` follows one of `(,=:[!&|?{};+-*%~^<>` or the start of file. Every
newline survives every substitution, so a line number taken from the stripped
text is the line number of the original file — the same discipline
`scrub_jinja` already uses on the template side.

Inline scripts get one more step: the template is scrubbed of Jinja first, then
everything outside a `<script>` body without a `src` is blanked, newlines kept.
That is what keeps the word `fetch()` inside the `{# … #}` comment at
`base.html:7` from being read as a call (§4, and the only uncovered raw hit).

### URL resolution

A URL argument resolves through, in order: string literal; template literal
with `${…}` collapsed; `+` concatenation, where a non-literal part becomes
`{param}`; and a ternary, which yields one candidate per branch. A bare
identifier is chased back to its `const|let|var` assignment inside the
enclosing function. A `window.location.origin` or `baseUrl` prefix is dropped;
query string and fragment are cut.

Two resolutions do the real work, and without them the pass is close to
worthless on this codebase:

* **Wrappers.** `fetchJson(url, options)` and `appendPinButton(wrap, kind, url)`
  are the project's two idioms: the endpoint is literal at the *call site*, the
  call pattern is one level down in the helper. The scanner finds a local
  function whose body calls a tracked pattern on one of its own parameters,
  records which parameter index carries the URL, and resolves that argument at
  every call site. Nine rows come from this (seven in `saa_section.js`, two in
  `chat.js`), and the verb comes from the call site's own options object where
  it has one.
* **`data-*` attributes.** `const url = el.dataset.pfSseUrl` is unresolvable in
  the script, but the template that writes `data-pf-sse-url` holds the literal.
  The attribute name is carried on the call, looked up in the template anchor
  index, and the attribute's value becomes the endpoint. Three rows —
  `/chat/stream/{param}`, `/scraper/runs/{param}/stream`,
  `/scraper/runs/{param}/cancel` — and in each case the attribute is both the
  endpoint and the trigger.

A ternary URL beside a ternary method is genuinely two call sites and is
emitted as two rows (`detail.html:873` — `POST …/positions` and
`PUT …/positions/{param}`). Two branches that normalise to the same endpoint
are one row, not two.

### The linking window

§2.4's "same function body if you can bracket it, else within 60 lines" needed
one correction to do what it intends. The innermost function body is the wrong
window on its own: a call inside
`btn.addEventListener("click", function () { … })` is bracketed by the handler,
while the `btn` it is bound to was selected *outside* it. So the walk steps out
of each enclosing callback — `addEventListener`, `forEach`, `.then`, `.map`,
`setTimeout`, `setInterval`, `on<event> =` — up to three levels, and stops at
the first named function body, which is where the element lookup lives. The
60-line window remains the fallback when nothing brackets the call. This took
linked calls from 15 to 23 of 44.

Within the window the nearest (last) binding wins, in priority order: a `data-*`
read, then `getElementById`, then `querySelector`/`querySelectorAll`/`closest`,
then a bare `dataset.<name>`. The event name comes from the nearest
`addEventListener` or `on<event> =`.

### The template anchor index

A second, independent regex pass over the templates collects every element
carrying an `id`, `class` or `data-*`, keyed three ways, with the visible text
taken from the element row the template pass already produced at that line.
Deliberately *not* a hook inside `ElementCollector`: the element rows had to
come out of a JS run byte-identical to a `--js-off` run, and the surest way to
guarantee that is to leave the collector alone. Simple selectors (`#id`,
`.class`, `[data-x]`, `[data-x='v']`, `tag[data-x]`) resolve; a descendant
combinator, comma group or pseudo-class is recorded as selector text and not
guessed at.

### What was not resolvable

Four calls keep `url = "<dynamic>"`, each for a sound reason (§5, F-UX-0b.3).

---

## 4. Numbers

44 JS rows added to `elements.csv` from 44 calls; 0 existing rows changed or
reordered.

| | |
|---|---:|
| JS rows | 44 |
| linked to a trigger element | 23 |
| unlinked | 21 |
| carrying visible text | 8 |
| URL unresolvable (`<dynamic>`) | 4 |
| resolved via a wrapper call site | 9 |
| resolved via a `data-*` attribute | 3 |
| `multi-trigger` | 1 |
| `sse` | 2 |
| `navigation` | 5 |

Verbs: GET 17, POST 13, DELETE 8, PUT 4, PATCH 2.
Events: submit 10, click 5, change 1, keydown 1.
Areas: investments 18, back_office 12, assistants 8, admin 2, unassigned 4.

Routes: **37 route rows have `js_callers` > 0**, 40 call attributions in total
(the remaining 4 calls are the unresolvable URLs, which match no route). The
count is per `(path, verb)`, not per path — `/investments/{investment_id}/navs/{nav_id}`
is two route rows and a DELETE caller is not a caller of the sibling PUT.

**Of the 29 routes attributed by handler module:**

| | count |
|---|---:|
| (a) now have a JavaScript caller found | **23** |
| (b) of those, have a linked trigger element | **14** |
| (c) still without any known caller | **6** |

The remaining 9 of the 23 have a caller located in a script but no trigger
element the linker could resolve: evidence of the route's existence and Area,
but not yet of what the user does to reach it.

The (c) list, verbatim:

```
* `GET /`
* `GET /investments/new`
* `GET /investments/{investment_id}/charts`
* `GET /investments/{investment_id}/edit`
* `PUT /investments/{investment_id}/cashflows/{cashflow_id}`
* `PUT /investments/{investment_id}/navs/{nav_id}`
```

**Area changes caused by the new edges: none.** This is the result after the
correction in F-UX-0b.4; the first implementation changed nine, eight of them
losses. That the final list is empty is the strongest available evidence that
the JS edges *confirm* the existing attribution rather than perturb it — and it
is what makes the `elements.csv` diff purely additive, since a changed template
Area would have rewritten existing rows.

### Call-site reconciliation (definition of done, item 3)

"Raw hits" counts lines matching check 6's pattern; occurrence counts equal line
counts in every file, so the two readings agree.

| file | raw hits | rows | uncovered raw | rows beyond raw |
|---|---:|---:|---|---|
| `web/static/js/chart_snapshot.js` | 0 | 0 | — | — |
| `web/static/js/chat.js` | 3 | 6 | — | 104, 118, 133 |
| `web/static/js/data_import_section.js` | 2 | 2 | — | — |
| `web/static/js/saa_section.js` | 5 | 12 | — | 176, 187, 209, 658, 719, 738, 847 |
| `web/static/js/scraper.js` | 2 | 2 | — | — |
| `web/static/js/section_nav.js` | 3 | 3 | — | — |
| `web/templates/investments/detail.html` | 13 | 14 | — | — |
| `web/templates/investments/edit.html` | 2 | 2 | — | — |
| `web/templates/investments/list.html` | 2 | 2 | — | — |
| `_partials/benchmarks_attribution_stage_a.html` | 1 | 1 | — | — |
| `web/templates/base.html` | 1 | 0 | **7** | — |
| **total** | **34** | **44** | | |

Every raw hit accounts for a row except one, and every surplus is explained:

* **`base.html:7` — the single gap.** The only hit is the word `fetch()` inside
  a Jinja `{# … #}` comment documenting the CSRF meta tag. It is prose, not a
  call, and the Jinja scrub removes it before the scanner runs. Correct
  exclusion.
* **`chat.js` +3, `saa_section.js` +7** — wrapper call sites, which are not raw
  hits because the call pattern is in the helper, not at the call site.
  `chat.js:133` is additionally an `hx-*` attribute set from script, which is a
  tracked shape but not one of check 6's patterns.
* **`detail.html` 13 hits → 14 rows** — line 873 is a conditional URL beside a
  conditional method, i.e. two real call sites (`POST …/positions` and
  `PUT …/positions/{param}`). All 13 hit lines are covered.
* **No external-URL `fetch` exists** in this codebase, so that anticipated class
  of gap does not arise.

### Guards

| guard | result |
|---|---|
| `--js-off` reproduces `elements.csv` | **byte-identical** to pre-change |
| `--js-off` reproduces `summary.md` | **byte-identical** to pre-change |
| `--js-off` reproduces `routes.csv` minus the new last column | **byte-identical** to pre-change |
| Two consecutive JS runs into separate directories | `diff -r` **identical** |
| In-repo artefacts vs a scratch run | `diff -r` **identical** |
| JS run: header + 1,181 existing rows | **byte-identical, order preserved** (`cmp` on the 1,182-line prefix) |
| `git diff --numstat docs/ux/inventory/elements.csv` | **44 added, 0 removed** |

Under `--js-off` the `js_callers` column is present and uniformly `0`; that is
the reading of §2.6's "minus the new last column", and it makes the guard prove
that the column is the *only* change to `routes.csv`.

### Toolchain (definition of done, item 4)

```
$ ruff check tools/ux_inventory.py
All checks passed!

$ ruff format --check tools/ux_inventory.py
1 file already formatted

$ pyright tools/ux_inventory.py
0 errors, 0 warnings, 0 informations
```

`pyright` is invoked by naming the file, per prompt rule §0.4: `[tool.pyright]
include` is island-scoped and `tools/` is not an island (see §1). The tool grew
from 1,924 to 3,226 lines. No test in `tests/` references `ux_inventory`, so
there is no suite to run against it.

---

## 5. Findings

**F-UX-0b.1 — check 6's per-file expectations overstate the static-JS call
surface roughly two-fold, against files that are provably the ones measured.**
All six line counts in check 4 match exactly, so the image is identical; yet
`chat.js` has 3 hits against ≥14 expected, `scraper.js` 2 against ≥8,
`saa_section.js` 5 against ≥6. Actual static total 15, expected ≥33. The
measured **grand** total across static *and* inline sources is 34 — within one
of the expected static-only figure, which suggests the expectations were
derived from the combined surface and attributed to the static files alone.
Consequence for the next run of this prompt: a low static-JS yield is not a
scanner bug. Recommend restating check 6 as a combined-surface expectation.

**F-UX-0b.2 — the endpoint is usually not at the call site, and a literal-only
scanner would have missed most of this Area.** Both of the project's scripts
that talk to more than one endpoint route through a local wrapper: `fetchJson`
in `saa_section.js` (7 endpoints, verbs POST/PUT/DELETE) and `appendPinButton`
in `chat.js` (2 endpoints). A scanner implementing §2.2's pattern table alone
would have produced two `<dynamic>` rows where there are nine real endpoints —
including the whole SAA configuration and asset-class write surface. The
wrapper resolution is the single highest-yield part of the pass and is flagged
`via:<wrapper>` so its rows stay distinguishable from directly observed ones.

**F-UX-0b.3 — four calls remain unresolvable, all for sound reasons; none is a
scanner limitation worth removing.**

* `chat.js:133` and `saa_section.js:62` — the wrapper *definitions*. Their URL
  is a parameter by construction. Emitted deliberately so that every raw hit
  accounts for a row; their real endpoints appear as the nine `via:` rows.
* `section_nav.js:210` and `:216` — the command palette. The URL comes from
  `row.getAttribute("data-url")` on a row the script itself built from a JSON
  payload fetched at runtime. There is no template attribute to follow, and no
  static analysis can recover these: the endpoint set is whatever
  `/api/cmd-search` returns. This is a genuine and permanent blind spot, and it
  is worth knowing that the command palette can navigate anywhere.

**F-UX-0b.4 — the JS edges initially destroyed Area attribution for eight
routes, through the handler-module consensus.** The first implementation
propagated a scripted `location.href` like a request. `data_import_section.js:66`
redirects to `/investments` after a successful import, and its trigger lives in
the Admin data-import partial — so `/investments` acquired `admin|investments`.
That broke the single-Area consensus in `by_module["web.routes.investments"]`,
and eight sibling routes that had inherited `investments` from it fell back to
`unassigned`. A net loss of evidence from an edge that was supposed to add
some. The fix applies the rule `propagate` already documents for plain `href`
("linking to a page is navigation; only a link that pulls in a fragment is
composition") to scripted navigations too: `js_edges_for` returns two buckets,
and the navigation bucket passes through the same page/URL-space filter the
`nav_urls` bucket does. After the fix, zero routes change Area. Worth recording
because the failure was silent — the artefacts still generated cleanly, and
only the `area change:` lines on stdout showed the damage.

**F-UX-0b.5 — two `PUT` endpoints have no caller in any template or script, and
are candidates for removal.** `PUT /investments/{investment_id}/navs/{nav_id}`
and `PUT /investments/{investment_id}/cashflows/{cashflow_id}` are in the
residual (c) list. Their sibling `DELETE` rows on the same paths *are* called
(`detail.html:374` and `:447`), and editing a NAV or cashflow goes through
`POST …/navs` and `POST …/cashflows` (`detail.html:599`, `:633`). So the pass
found every neighbouring endpoint of these two and not these two. That is
suggestive rather than conclusive — the inventory proves only "no caller found
in the templates and scripts it reads", and a bot or API client is not in
scope — but it is a concrete lead worth one look. The other four residuals
(`GET /`, `/investments/new`, `/investments/{id}/charts`,
`/investments/{id}/edit`) are all whole-page routes reached by ordinary `href`
navigation, which the tool deliberately does not propagate; they need no
explanation beyond that.

**F-UX-0b.6 — no script calls a route that does not exist.** Checked: every one
of the 40 resolved URLs matches a route in the table. Nothing to report, which
is itself the useful result.

**F-UX-0b.7 — the next gap is injected copy, and it is 22 string literals.**
User-visible text assigned through `textContent`, `innerHTML`,
`insertAdjacentHTML`, `placeholder` and `title` is still invisible to the
inventory. Per script:

| script | injected literals |
|---|---:|
| `saa_section.js` | 7 |
| `chat.js` | 6 |
| `scraper.js` | 4 |
| `chart_snapshot.js` | 3 |
| `section_nav.js` | 2 |
| `data_import_section.js` | 0 |
| **total** | **22** |

Examples: `"Unsaved changes"`, `"All changes saved"`,
`"Fix the highlighted cell before saving."` (`saa_section.js`);
`"Pin to case…"` (`chat.js`); `"Keyword name"` (`scraper.js`);
`"<li class=\"pf-palette__empty\">No matches.</li>"` (`section_nav.js`).
Note that `chart_snapshot.js` carries 3 of these despite having **zero** call
sites — check 6's `0` says nothing about whether a script contributes copy.
Sizing this was in scope; reading it was not, and 22 literals is a small enough
body to be worth a successor prompt rather than a tolerated blind spot.

---

## 6. Open questions

1. **Accept or strike the two extra flags?** `via:<wrapper>` and
   `url-from-attribute` are provenance beyond §2.5's enumerated list (§1). My
   recommendation is to keep `via:` — without it a derived row is
   indistinguishable from an observed one — and to treat `url-from-attribute`
   as optional.
2. **Should an *unlinked* call in an inline script carry its own template's
   Area as an edge?** §2.5 says an unlinked call adds no edge, and that is what
   is implemented. But a `fetch` inside `investments/detail.html` is
   unambiguous template evidence whether or not a trigger element was resolved,
   and 21 of the 44 rows are unlinked. Loosening this is a one-line change with
   a real risk of the F-UX-0b.4 failure mode, so it should be a deliberate
   decision measured against the `area change:` output, not a default.
3. **Restate check 6 before this prompt is run again?** Per F-UX-0b.1 the
   expectations do not hold as written and will mismatch on every future run.
4. **Is F-UX-0b.5 worth acting on?** Confirming two unused `PUT` endpoints needs
   a look at the bot and any API consumers, which is outside what the inventory
   can see.
5. **Does the ADR-0110 addendum (§1) block anything?** Not this work. It blocks
   only the convenience of `pyright` picking `tools/` up without being told.
