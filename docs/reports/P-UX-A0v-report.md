# P-UX-A0v — local vendor bundle and the `pf_icon` Jinja global

**Date:** 2026-09-22 · **Branch:** `main` · **Strand:** UX A-0 (shell and tokens)

Every third-party asset the web shell loads now comes from the tree, as
ADR-0037 §9 foresees for the local vendor bundle. The CDN `<script>` and
`<link>` lines in `base.html` are replaced by `/static/vendor/` paths at the
same pinned versions, and the Lucide icon set (ISC) is vendored as the 38 SVG
files the shell will need, read by one Jinja global `pf_icon(name, label=None)`.

No template body, no component sheet and no route changed. **Nothing calls
`pf_icon` yet** — the sidebar takes it in the next prompt of this strand.

---

## OPERATOR ACTION REQUIRED

### 1. Staging — read this before running `git add`

The prompt's staging line is `git add … web/static/vendor …`. That directory
now holds **two strands' payloads**: the four packages this prompt vendored,
and the IBM Plex Sans web fonts placed there for the typography prompt, which
is still uncommitted and whose code-side work (the `@font-face` rules, the
stylesheet link and the preload insertion in `base.html`) has not been written.
A bare `git add web/static/vendor` therefore stages the fonts into this commit
as well.

Two ways out — pick one:

**(a) Stage only this strand's payload** (recommended; leaves the fonts
untracked for their own commit):

```
git add web/templates/base.html web/main.py web/icons.py \
        web/static/vendor/README.md web/static/vendor/htmx-1.9.12 \
        web/static/vendor/plotly-2.35.2 web/static/vendor/tabulator-5.6.1 \
        web/static/vendor/lucide \
        tests/web/test_static_assets.py tests/web/test_icons.py \
        repomix.config.json docs/reports/P-UX-A0v-report.md
git commit -m "build(web): vendor HTMX, Plotly, Tabulator and the Lucide icon set locally; pf_icon Jinja global (UX A-0, P-UX-A0v)"
```

**(b) Commit the typography prompt first**, then the prompt's own block works
unchanged:

```
git add web/templates/base.html web/main.py web/icons.py web/static/vendor \
        tests/web/test_static_assets.py tests/web/test_icons.py repomix.config.json \
        docs/reports/P-UX-A0v-report.md
git commit -m "build(web): vendor HTMX, Plotly, Tabulator and the Lucide icon set locally; pf_icon Jinja global (UX A-0, P-UX-A0v)"
```

Note that `web/static/vendor/` is **tracked** — `.gitignore` was not touched.
The staged payload is ~5.2 MB, of which 4.4 MB is `plotly.min.js`.

### 2. Walk after the commit

Start the dev server, open the browser's network tab and filter to
**"3rd-party"**. Load **Front Office › Charts** and **Back Office ›
Benchmarks**. Expected: both render as before, and the third-party filter
stays **empty** — no request leaves localhost.

The five vendored assets were already proven to serve through the app's
`/static` mount (see "Gates" below), so a failure here would point at the
browser cache rather than the bundle; hard-reload once before judging.

### 3. No atlas run

Nothing visual changed. The same bytes are executed, from a different origin.

---

## Verify-first values observed (1–16)

| # | Expected | Observed | Result |
|---|---|---|---|
| 1 | filtered `git status --porcelain` empty | empty (only `?? web/static/vendor/`) | ✅ |
| 2 | last commit ends `(UX A-0, P-UX-A0a)` | `5328291 fix(ux): atlas drives nested lazy loaders … (P-UX-1c)`; no commit mentioning that prompt exists on any branch | ❌ **waived by the operator**, who is handling commits after this run — see "Deviations" |
| 3 | `htmx.js htmx.min.js LICENSE` + `ext/sse.js` | all four present | ✅ |
| 4 | `htmx.min.js` SHA-384 | `ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2` — exact match to the first `integrity=` the template carried; **byte-identical to the CDN copy** | ✅ |
| 5 | `ext/sse.js` SHA-384 | `OZrRw8/Zvv0VFGJJF6TN3gABVZvvO60Xz7RWkQKAmoRe+t1dc5J/ySJvXbpL+N+Q` — exact match to the second | ✅ |
| 6 | sizes, three LICENSEs, every §2.4 SVG | `plotly.min.js` 4,558,696 B; `tabulator.min.js` 419,703 B; `tabulator.min.css` 26,870 B; LICENSE in htmx / plotly / tabulator / lucide; **all 38 SVGs present, none missing** | ✅ |
| 7 | `plotly.js v2.35.2` | one hit | ✅ |
| 8 | `Tabulator v5.6.1` | `Tabulator v5.6.1` | ✅ |
| 9 | five CDN hits at 39, 69, 72, 79, 80 | five hits at **35, 65, 68, 75, 76** — the pre-insertion numbers the check names in parentheses, because the typography prompt's four-line preload insertion has not landed. All five text anchors present and correct, no sixth, none missing; the anchors are what the edit bound to. | ⚠️ anchors ✅, positions pre-insertion |
| 10 | only `tests/web/test_static_assets.py:76` | **two** hits: that line, plus `web/static/vendor/plotly-2.35.2/plotly.min.js:8`. Count mismatch whose expected text anchor matches → reported, run continued, per the §1 header rule. See "The Plotly `topojsonURL` string". | ⚠️ reported |
| 11 | `74:`, `79:`, `87:` | exactly those three | ✅ |
| 12 | `476:` | `476:    templates.env.globals["pf_section_title"] = section_title` | ✅ |
| 13 | `web/icons.py`, `tests/web/test_icons.py` absent | both absent | ✅ |
| 14 | two-key repomix config | `{"security": {"enableSecurityCheck": false}}` | ✅ |
| 15 | `viewBox="0 0 24 24"` in `x.svg` | present. The file opens `<!-- @license lucide-static v1.47.0 - ISC -->`, then a multi-line `<svg>` with `stroke-width="2"` — the newer shape the check anticipates. The licence comment is the manifest's version source. | ✅ |
| 16 | quote verbatim | below | ✅ |

### Check 16 — the one-section-per-view evidence, verbatim

From `web/static/vendor/htmx-1.9.12/htmx.js` (unminified, 1.9.12):

```javascript
426:        function isScrolledIntoView(el) {
427:            var rect = el.getBoundingClientRect();
428:            var elemTop = rect.top;
429:            var elemBottom = rect.bottom;
430:            return elemTop < window.innerHeight && elemBottom >= 0;
431:        }
```

```javascript
1605:        function maybeReveal(elt) {
1606:            if (!hasAttribute(elt,'data-hx-revealed') && isScrolledIntoView(elt)) {
1607:                elt.setAttribute('data-hx-revealed', 'true');
1608:                var nodeData = getInternalData(elt);
1609:                if (nodeData.initHash) {
1610:                    triggerEvent(elt, 'revealed');
1611:                } else {
1612:                    // if the node isn't initialized, wait for it before triggering the request
1613:                    elt.addEventListener("htmx:afterProcessNode", function(evt) { triggerEvent(elt, 'revealed') }, {once: true});
1614:                }
1615:            }
1616:        }
```

The two lines where `initScrollHandler` binds its listener, with the poll they drive:

```javascript
1586:        var windowIsScrolling = false // used by initScrollHandler
1588:        function initScrollHandler() {
1589:            if (!scrollHandler) {
1590:                scrollHandler = function() {
1591:                    windowIsScrolling = true
1592:                };
1593:                window.addEventListener("scroll", scrollHandler)
1594:                setInterval(function() {
1595:                    if (windowIsScrolling) {
1596:                        windowIsScrolling = false;
1597:                        forEach(getDocument().querySelectorAll("[hx-trigger='revealed'],[data-hx-trigger='revealed']"), function (elt) {
1598:                            maybeReveal(elt);
1599:                        })
1600:                    }
```

and the call site that arms it, at 1875–1878:

```javascript
1875:            } else if (triggerSpec.trigger === "revealed") {
1876:                initScrollHandler();
1878:                maybeReveal(elt);
```

**What the text settles.** `isScrolledIntoView` is a plain viewport-intersection
test, not a one-section-per-view selector: it is true for *every* element with
any part on screen, so on a long-scroll Area page all simultaneously visible
`revealed` sections fire together. Three properties follow directly:

1. **Latching, not toggling.** `data-hx-revealed` is set once at 1607 and never
   cleared, so a section loads on first exposure and never reloads on
   scroll-back. The mechanism is one-shot per element.
2. **Polled, not event-driven.** The scroll listener only raises a flag (1591);
   the sweep runs on a `setInterval` and re-queries the document each tick. A
   fast scroll past a section still reveals it, because the flag survives to the
   next tick.
3. **Initialization-ordered.** A node not yet processed defers its `revealed`
   event to `htmx:afterProcessNode` with `{once: true}` (1613) — which is why
   nested lazy loaders need the parent settled first, the same ordering the
   atlas work had to accommodate.

If the intent is genuinely *one section per view*, htmx's `revealed` does not
provide it and no configuration of it will; that needs an `IntersectionObserver`
with a threshold and a most-visible-wins arbiter layered above. Left as a design
call — nothing was acted on here.

---

## Diff and gates

### Files

Modified (4): `web/templates/base.html`, `web/main.py`,
`tests/web/test_static_assets.py`, `repomix.config.json`.
New: `web/icons.py`, `tests/web/test_icons.py`, `web/static/vendor/**` (51 files
excluding the separate font bundle), this report.
`config/`, `services/chart_specs/` and `web/static/css/` are untouched, as gate 5
requires.

`base.html` lost 30 lines and gained 20: the two-line Tabulator stylesheet link
became one (now line 34), and the 18-line script block with its two
`integrity=`/`crossorigin` pairs became a 9-line vendored block (59–66). The
`theme.css` link and the component sheets did not move.

### Gates

| # | Gate | Result |
|---|---|---|
| 1 | no `unpkg.com` / `cdn.plot.ly` / `integrity=` in `web/templates` | **no hits** ✅ |
| 2 | `pytest tests/web/test_static_assets.py tests/web/test_icons.py -q` | **22 passed** in 0.75 s, both DB-free ✅ |
| 3 | `pytest tests/regression/test_section_catalogue_matches_body_partials.py tests/tools -q` | **46 passed** — the shell still imports ✅ |
| 4 | `ruff check` + `ruff format --check` on the four Python files | **clean** ✅ · `pyright web/icons.py web/main.py` advisory: `web/icons.py` **clean**; one pre-existing error at `web/main.py:330` (`start_bot(database_url=…)` takes `str`, settings give `str \| None`), untouched by this change and outside the ADR-0110 pyright island, which covers only four `services/` packages | ✅ |
| 5 | diff surface | exactly as listed above ✅ |
| 6 | byte-identity | htmx and SSE by SHA-384 (checks 4–5); Plotly and Tabulator by version banner (checks 7–8) ✅ |
| 7 | `python -c 'from web.icons import ICONS, pf_icon; [pf_icon(n) for n in ICONS]; print(len(ICONS))'` | prints **38**, no error ✅ |
| 8 | operator walk | pending — see above. **No `confirm(`, `alert(` or `hx-confirm` was added and no `type="number"` was touched**; the only `alert` substrings introduced are the Lucide stems `triangle-alert` and `octagon-alert`. |

Beyond the listed gates, each vendored asset was fetched through the app's own
`/static` mount over ASGI, with no database configured:

```
200     48,101 B  application/javascript    /static/vendor/htmx-1.9.12/htmx.min.js
200     10,081 B  application/javascript    /static/vendor/htmx-1.9.12/ext/sse.js
200  4,558,696 B  application/javascript    /static/vendor/plotly-2.35.2/plotly.min.js
200    419,703 B  application/javascript    /static/vendor/tabulator-5.6.1/tabulator.min.js
200     26,870 B  text/css; charset=utf-8   /static/vendor/tabulator-5.6.1/tabulator.min.css
```

Byte counts match the files on disk, so the mount resolves the paths the
template now names.

### The Plotly `topojsonURL` string

The second hit under check 10 is inside the vendored bundle:

```
plotly.min.js:8 … topojsonURL:{valType:"string",noBlank:!0,dflt:"https://cdn.plot.ly/"} …
```

`unpkg.com` ×1 and `cdn.plot.ly` ×1 occur in that file; `cdnjs` does not. This is
Plotly's own default for fetching topojson boundary files, consulted **only**
when a geo trace renders. It cannot fire here:

* `grep -rn 'choropleth\|scattergeo\|"geo"\|scattermapbox\|choroplethmapbox\|topojson'`
  across `services/chart_specs/` and `web/` → **no hits**;
* the 28 chart specs emit `bar`, `heatmap`, `line`, `pie`, `scatter`, `treemap`
  only — geographic allocation is drawn as a treemap, not a map.

So gate 8 will hold and no `Plotly.setPlotConfig` shim is needed. It is recorded
in `web/static/vendor/README.md` so a future geo chart does not reintroduce an
egress silently. **Check 10 itself needs re-keying** for any future run: its grep
covers `web/`, where the bundle now lives, so it needs
`| grep -v '^web/static/vendor/'` to return the single expected line. Gate 1 is
unaffected — it greps `web/templates` only.

### SRI

`base.html` no longer sets `integrity=`. Subresource Integrity guards bytes
fetched over the network; these are read from the same checkout as the template,
so the attribute would only pin the shell to a hash a legitimate version bump has
to remember to update. The hashes did their job once — they are what proved the
vendored htmx files identical to the CDN copies — and both are recorded in
`web/static/vendor/README.md` with the command to re-check them.

---

## Deviations from the prompt

1. **Check 2 waived.** The typography prompt is still uncommitted and its
   code-side work unwritten; the operator waived the gate and is handling commits
   after this run. Two consequences are visible in this report: check 9 observes
   pre-insertion line numbers, and there was no preload link in `base.html` to
   preserve. The §2.1 edit bound to its text anchors, so the preload insertion
   can still land ahead of the vendored block without touching it. The staging
   hazard this creates is the subject of "OPERATOR ACTION REQUIRED" above.
2. **§2.5's "keep the order test as is" could not hold literally.** That test
   located the stylesheet with
   `base.find("tabulator-tables@5.6.1/dist/css/tabulator.min.css")` — a string
   this change removes, so leaving it untouched would have failed it at
   "Tabulator base CSS link is missing." rather than passing. The search string
   is re-pointed at the vendored path (now a shared `_VENDOR_TABULATOR_CSS`
   constant), `tabulator_cdn_pos` is renamed `tabulator_base_pos` because it no
   longer names a CDN, and one comment line records why the anchor changed. The
   cascade assertion and its failure message are unchanged in substance.
3. **`@functools.lru_cache` → `@functools.cache`.** The prompt specifies
   `lru_cache`, but `@functools.lru_cache(maxsize=None)` trips ruff's UP033, and
   ruff-clean is gate 4. `functools.cache` *is* `lru_cache(maxsize=None)` and
   exposes the same `cache_info()`, which the §2.6 cache test reads. The codebase
   had no prior use of either, so no local convention was overridden.

---

## Open questions

1. **Lucide release version — answered.** `lucide-static` **v1.47.0**, agreed by
   the `@license` comment in every SVG and by the `package.json` left beside them.
   Recorded in `web/static/vendor/lucide/MANIFEST.md`.
2. **Icons the pinned release does not ship — none.** All 38 product names in
   §2.4 resolve. For the four icons Lucide renamed, v1.47.0 ships the **newer**
   stem in every case, so `ICONS` records `send-horizontal`, `triangle-alert`,
   `octagon-alert` and `ellipsis` rather than the bracketed alternates `send`,
   `alert-triangle`, `alert-octagon` and `more-horizontal`.
3. **Icon sizing at other scales.** `pf_icon` takes `size=16` by default and
   scales the `width`/`height` while the `viewBox` stays `0 0 24 24`. The stroke
   is fixed at 1.5 px in the 24-unit coordinate system, so it renders thinner at
   16 px than at 24 px. If the shell wants an optically constant stroke across
   sizes, that is a parameter this helper does not yet take — worth deciding when
   the first non-16 px use appears rather than guessing now.
4. **Check 10's grep needs the vendor filter** before any re-run of this
   verification, as set out above.
