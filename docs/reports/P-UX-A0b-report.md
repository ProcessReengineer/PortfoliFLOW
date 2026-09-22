# P-UX-A0b — Shell frame: one section per view, second-level navigation, sticky view header, `intersect` loaders

**Date:** 2026-09-22 · **Branch:** `main` (no branch created, no commit, no push) ·
**Tree at start:** clean, at `b6bfeee` (the fused PB-H6 tip; `git show --stat HEAD`
lists `docs/reports/P-UX-A0a-report.md`, the variant check 2 accepts).

Every area now shows **one section at a time**. The URL fragment selects it, the
area's landing section is shown by default, and the other sections stay in the DOM
under the `hidden` attribute and never load. The sidebar gained a second level, each
section gained a sticky view header, the right-edge dot strip retired, and the 31
lazy loaders moved from `revealed` to `intersect once`.

---

## OPERATOR ACTION REQUIRED

### 1. Stage and commit

```
git add web/shell.py web/routes/areas.py web/templates/areas/_section.html web/templates/_partials/sidebar.html \
        web/templates/base.html web/templates/_partials/area_fragment.html web/static/js/shell.js \
        web/static/js/section_nav.js web/static/css/layout.css web/templates/_partials/ tests/web/ \
        tools/ux_atlas.py docs/reports/P-UX-A0b-report.md
git rm web/templates/_partials/section_indicator.html
git commit -m "feat(shell): one section per view with fragment routing and landing views, two-level navigation with icons, sticky view header, lazy loaders on intersect (UX A-0, P-UX-A0b)"
```

### 2. Re-seed the dev database

Gate 5 ran against live Postgres, and its fixtures `TRUNCATE` the tenant and user
tables. Before the walk:

```
portfoliflow bootstrap
```

### 3. Browser walk (gate 9)

Start `portfoliflow-web` and check, at `http://minathena-capital.localhost:8000`:

- [ ] `/transactions` opens on the **Blotter**, not on New transaction.
- [ ] Network tab on load: **one** `blotter` request. No `history` request, no
      `new`-section request.
- [ ] Putting `#history` in the URL switches the view and issues **one** `history`
      request; switching back to `#blotter` issues none (the loader is `once`).
- [ ] The sidebar's second level marks the current section (accent bar + raised
      background), and following one of its links switches the view.
- [ ] Browser back / forward buttons move between sections.
- [ ] `Ctrl K` and the view header's **Search** control open the same dialog.
- [ ] The page scrolls as a document — the sidebar and the view header stay put,
      the status bar stays on the bottom edge, and there is no inner scrollbar.
- [ ] `/front-office` and `/back-office` still draw their charts (the loaders fire
      on `intersect`, which is what a landing section triggers at page load).
- [ ] At 1280 px with the browser at 125 % zoom, the view header holds on one row.
- [ ] Collapse the sidebar: the icons stay, the labels and the second level go.

### 4. Re-run the atlas

```
python tools/ux_atlas.py --bands 1200
```

**Expect fewer bands than the last run.** The atlas captures each area's **landing
section only** — it has no way to walk sections by fragment yet. That is the known,
accepted consequence stated in the prompt's §0; P-UX-A0t teaches it the walk. The
only atlas change here is the loader selector (below).

---

## Verify-first — all 18 checks passed

| # | Expected | Found |
|---|---|---|
| 1 | `git status --porcelain` empty | empty |
| 2 | split history **or** fused `b6bfeee … (PB-H6)` whose stat lists `P-UX-A0a-report.md` | **fused variant**: `b6bfeee test: make CLI help and import-contract tests CI-independent (PB-H6)`, `e2fd660 build(web): … (UX A-0, P-UX-A0v)`; `git show --stat HEAD` lists `docs/reports/P-UX-A0a-report.md` ✅ |
| 3 | `601` / `42` | `601 /home/soenke/DC-UX-D/shared.css`, `42` |
| 4 | `31` | `31` |
| 5 | `7` | `7` — charts 305, statistics 466, scenario_results 258, portfolio_review 567, portfolio_analysis 578, planning_desk 678 and 2113 |
| 6 | `269:UNFIRED_LOADER_SELECTOR = f'[hx-trigger*="revealed"]:not([{FIRED_ATTR}="1"])'` | exact match at line 269 |
| 7 | `103`, `116`, `165`, `271` | `103` `class SectionMeta`, `116` `    title: str`, `165` `"transactions": (`, `271` `def section_index_for` |
| 8 | Transactions tuple, `blotter` present | `new` → `blotter` → `history`; `blotter` present ✅ |
| 9 | `173:` | `173:        "section_index": section_index_for(area_slug),` |
| 10 | `base.html:83`, `base.html:102`, `area_fragment.html:19` | all three exact |
| 11 | three hits | `23` `<section class="pf-section"`, `24` `<header class="pf-section__header">`, `25` `<h2 class="pf-section__title"` |
| 12 | ≥ 6 | `6`. Test files touching those classes: `test_auth_surface_layout.py test_sidebar_glyph_and_auth_polish.py test_planning_desk.py test_section_navigation.py test_shell_sidebar_and_areas.py test_watch_desk.py` — see "Tests that had to move" below |
| 13 | record hyphen vs underscore | **underscore**: `front_office`, `back_office`, `assistants`, `planning_desk`, `investor_communication`, `watch_desk`, `cases`, `transactions`, `admin`. §2.4's `slug.replace("_", "-")` is therefore load-bearing |
| 14 | `45`, `26`, `77`, `76` | all four exact; scroll-spy block was lines 22–77 |
| 15 | name the palette-open function | `openPalette()` at line 219, hotkey gate `isPaletteHotkey()` at 237 (`event.metaKey \|\| event.ctrlKey` at 241), `paletteDialog.showModal()` at 223 |
| 16 | `703` / `0` | `703` lines, `0` hex |
| 17 | the `intersect` branch exists | quoted verbatim below |
| 18 | all absent | all three absent |

### Check 17 — htmx 1.9.12's `intersect` branch, verbatim

`web/static/vendor/htmx-1.9.12/htmx.js`, lines 1879–1897:

```js
            } else if (triggerSpec.trigger === "intersect") {
                var observerOptions = {};
                if (triggerSpec.root) {
                    observerOptions.root = querySelectorExt(elt, triggerSpec.root)
                }
                if (triggerSpec.threshold) {
                    observerOptions.threshold = parseFloat(triggerSpec.threshold);
                }
                var observer = new IntersectionObserver(function (entries) {
                    for (var i = 0; i < entries.length; i++) {
                        var entry = entries[i];
                        if (entry.isIntersecting) {
                            triggerEvent(elt, "intersect");
                            break;
                        }
                    }
                }, observerOptions);
                observer.observe(elt);
                addEventListener(elt, handler, nodeData, triggerSpec);
            } else if (triggerSpec.trigger === "load") {
```

A real `IntersectionObserver`, which reports nothing for a `display: none` element —
unlike the `revealed` branch immediately above it, whose `maybeReveal` →
`isScrolledIntoView` is a bare `getBoundingClientRect()` test that a zero rect
passes. That difference is the whole reason for §2.9.

---

## What changed

### `web/shell.py` — the landing view

- `SectionMeta` gains `landing: bool = False`, documented as "the section shown when
  the area is opened without a fragment; at most one per area, else the first entry".
- `_SECTIONS_BY_AREA["transactions"]` flags `blotter`. No other area is flagged.
- New `landing_section_for(area_slug) -> str`: the flagged slug, else the first;
  `LookupError` for an unknown area, in `section_title`'s style.
- `section_index_for` now emits `{"slug", "title", "landing"}`, `landing` a
  `"true"`/`"false"` string so the dict type stays `dict[str, str]`.

**One decision worth naming.** `section_index_for` carries the **resolved** landing
from `landing_section_for`, not the raw `SectionMeta.landing` flag. Emitting the raw
flag would mark exactly one area (Transactions) and leave the other eight with no
`aria-current` on their second level at all — the sidebar has to mark the current
section on every area, not only on the one that carries a flag. The raw flag is
still reachable on `SectionMeta` and is what
`test_at_most_one_landing_flag_per_area` asserts against.

### `web/routes/areas.py`

One context key and one import name. Nothing else.

### `web/templates/areas/_section.html`

`hidden` on every section but the landing one, and the old `pf-section__header` block
replaced by the `pf-view__head` view header: title row (title · pill · as-of) and
tools row (command search · action slot). `section_as_of` and
`section_primary_template` are new optional parameters — the slots exist; no call
site fills them yet.

### `web/templates/_partials/sidebar.html`

Lucide icons on the first level (`pf_icon(slug.replace("_", "-"))`; Platform Admin
uses `pf_icon("platform-admin")`), and the second level — the active area's sections
— rendered directly after the active `<a>`. Collapse and sign-out glyphs are icons
now. Every existing class, the `id="pf-sidebar"`, and the whole `hx-*` set are
untouched, so the OOB swap and the active-state tests still hold.

### `web/templates/base.html`, `_partials/area_fragment.html`

Indicator includes removed from both; `shell.js` added after `section_nav.js`.
`web/templates/_partials/section_indicator.html` deleted.

### `web/static/js/section_nav.js` — 324 → 278 lines

The scroll-spy (`bindScrollSpy`, its observer, both listener registrations) is gone.
The palette is untouched, and `openPalette()` is now also reachable from a click on
`[data-pf-palette-open]`, delegated on `document` because every area swap re-renders
the button.

### `web/static/js/shell.js` — new, 77 lines, no dependencies

`sections()` / `show(slug)` / `resolve()`, run once at load and again on
`hashchange` and on an `htmx:afterSwap` whose target is `#shell-main`. It issues no
request and touches no `hx-*` attribute — pinned by `test_shell_js_issues_no_request`.

The ordering comment in the file is the load-bearing part: the script is `defer`red,
so it runs **after** the DOM is parsed but **before** htmx's own `DOMContentLoaded`
handler processes the body. The non-landing sections are therefore already `hidden`
when htmx arms their observers, which is why their loaders never fire.

### `web/static/css/layout.css` — 703 → 813 lines

Shell blocks rewritten on the generator's `--ui-*` tokens, geometry taken from
`~/DC-UX-D/shared.css` with the mock's class names mapped onto the product's. The
`.pf-section-indicator*` block (old 365–425) and the old
`.pf-section__header` / `.pf-section__title` rules are gone.

**One rule in there is not cosmetic and was not in the prompt:**

```css
.pf-section[hidden] {
    display: none;
}
```

An author `display` declaration beats the user-agent's `[hidden] { display: none }`
whatever the specificity — author rules outrank UA rules outright. So
`.pf-section { display: flex }` alone would have left **every** hidden section on
screen, and htmx would then have armed and immediately fired all 31
`intersect once` loaders at page load: the precise failure §0 cites as the reason
for the `revealed` → `intersect` move, reintroduced through the back door. The
project already knows this idiom (`.chat-voice[hidden]` in `chat.css`). Pinned by
`test_layout_css_makes_hidden_sections_actually_hidden`, because one deleted rule
brings the failure back and nothing else in the suite would notice.

### Loaders — 31 occurrences in 28 templates

`hx-trigger="revealed"` → `hx-trigger="intersect once"`, exact-string, nothing else
on those lines. The one Planning Desk `load` trigger and the negative-cash shell are
untouched.

<details>
<summary>The 28 files (31 occurrences)</summary>

```
_partials/benchmarks_attribution_section.html        1
_partials/benchmarks_attribution_section_lazy.html   1
_partials/cases_archive_lazy.html                    1
_partials/cases_open_lazy.html                       1
_partials/cases_recently_closed_lazy.html            1
_partials/charts_section.html                        3
_partials/charts_section_lazy.html                   1
_partials/limits_section.html                        1
_partials/limits_section_lazy.html                   1
_partials/overview_section.html                      1
_partials/overview_section_lazy.html                 1
_partials/planning_desk_cash_flow_planning_lazy.html 1
_partials/planning_desk_scenario_results.html        1
_partials/portfolio_analysis_section.html            1
_partials/portfolio_analysis_section_lazy.html       1
_partials/portfolio_review_section.html              2
_partials/portfolio_review_section_lazy.html         1
_partials/provider_credentials_section_lazy.html     1
_partials/saa_section_lazy.html                      1
_partials/scraper_section_lazy.html                  1
_partials/statistics_section.html                    1
_partials/statistics_section_lazy.html               1
_partials/tenant_users_section_lazy.html             1
_partials/transactions/_blotter_lazy.html            1
_partials/transactions/_history_lazy.html            1
_partials/watch_desk_briefing_lazy.html              1
_partials/watch_desk_calibration_lazy.html           1
_partials/watch_desk_journal_lazy.html               1
```
</details>

### `tools/ux_atlas.py` — the selector line only

```python
UNFIRED_LOADER_SELECTOR = (
    f'[hx-trigger*="revealed"]:not([{FIRED_ATTR}="1"]),'
    f'[hx-trigger*="intersect"]:not([{FIRED_ATTR}="1"])'
)
```

Both spellings, each with its own `:not(...)`, because a comma in a selector list
does not distribute a suffix. `revealed` stays in so a stray one would be reported
as unfired rather than silently ignored.

---

## Deliberate deviations from the prompt

Four, each small, each with its reason. None of them is a silent change.

**1. `.pf-section` lost its card chrome.** §2.8 specifies
`.pf-view__head { … background: var(--ui-background-primary) … }`, and that only
reads correctly if the view's own surface is `--ui-background-primary`. The old
`.pf-section` was a bordered `--pf-bg-surface` card whose header was cut into it
with negative margins and a blur; with one view on screen and its header pinned to
the *viewport*, the card frame has nothing left to frame. `.pf-section` is now the
view container — flex column, gap, `min-width: 0` — and `.pf-main`'s gutter supplies
the page margin the card padding used to. Body content and its component classes are
untouched.

**2. `.pf-statusbar` became `position: sticky; bottom: 0`.** §2.8 says to keep the
statusbar rules, and its appearance is kept — but its *placement* had to move twice
over: the shell grid no longer names its areas (so `grid-column: 1 / -1` replaces
`grid-area: statusbar`), and the document scrolls now (so without `sticky` the bar
would fall below the fold on any view taller than the viewport, where it used to
ride the viewport-high grid). Sticky is what *preserves* the existing behaviour
here, not what changes it.

**3. The sidebar keeps its wrapping labels.** The mock's `.pf-nav__item` carries
`white-space: nowrap`; `.pf-sidebar__item` must not, because
`test_sidebar_label_does_not_force_truncation` pins the 6F-2 polish-loop decision
that "Investor Communication" wraps rather than truncates. `min-height` +
`line-height: 1.25` instead of the mock's fixed `height`.

**4. The collapsed sign-out icon carries an accessible name.** Everything else in
§2.4 is `pf_icon(name)`, decorative. The sign-out button has no `aria-label` of its
own (the collapse button does), and in the collapsed state its text span is
`display: none` — so a decorative icon there would leave the control nameless.
It renders `pf_icon("sign-out", "Sign out")`, which is the helper's own path for an
icon-only control. Gate 8 asks for exactly this.

Also worth stating plainly: the sidebar glyphs are now visible at **both** nav
widths, not only when collapsed. That follows from §2.4 putting icons on the first
level; the old `display: none` default existed only because a bare letter beside a
label is noise.

---

## Tests

### New

- **`tests/web/test_shell_catalogue.py`** — DB-free, imports `web.shell` only.
  Transactions lands on `blotter`; every other area lands on its first slug; at most
  one `landing=True` per area; `section_index_for` marks exactly one entry `"true"`
  and its keys are `{slug, title, landing}`; unknown area → `LookupError` from
  `landing_section_for` and `[]` from `section_index_for`. **30 passed.**
- **`tests/web/test_shell_sections.py`** — DB-bound, `web_client` idiom from
  `test_logo_partial.py`. `/transactions` renders `blotter` visible and the other two
  `hidden`; `/front-office` shows its first section only; the second level lists one
  link per catalogue section with `aria-current` on the landing; no
  `pf-section-indicator`; `shell.js` linked; one `pf-view__head` per section;
  `shell.js` contains no `fetch(` / `htmx.ajax` / `hx-get`; and
  `.pf-section[hidden]` restates `display: none` (see the `layout.css` note above —
  this is the one test guarding the whole scheme).

### Rewritten rather than deleted

- **`tests/web/test_section_navigation.py`** — the module of the surface this prompt
  replaces. The two indicator tests became assertions of **absence** (full page and
  HTMX fragment); `test_section_nav_js_uses_intersection_observer` became
  `test_shell_js_replaced_the_scroll_spy` (`shell.js` linked **and** `bindScrollSpy`
  gone from `section_nav.js`); `test_sticky_header_theme_tokens_present` was dropped
  with the tokens it guarded; `test_layout_css_sticky_section_header` became
  `test_layout_css_sticky_view_header` plus a new
  `test_layout_css_main_does_not_scroll` (the `overflow` removal is load-bearing and
  deserved its own pin); the heading regex follows `pf-section__title` →
  `pf-view__title`. Every palette, `Ctrl K`, `cmd-search` and section-anchor test is
  kept unchanged.
- **`test_sidebar_uses_single_letter_glyphs` → `test_sidebar_uses_icon_glyphs`** in
  `tests/web/test_sidebar_glyph_and_auth_polish.py`. Each `pf-sidebar__glyph` holds
  exactly one `<svg class="pf-icon"` and nothing beside it. The `_LEGACY_CODES`
  proximity check retired with the letters — a two-letter code cannot come back
  through an inline SVG.

### Adjusted

- The 7 loader assertions, plus the comment at `test_charts_section_routes.py:304`
  and the two docstring mentions at `test_portfolio_review_section_routes.py` 30 and
  548 — 10 replacements in all.
- `tests/web/test_planning_desk.py` — two assertions keyed on the indicator's
  `data-section` attribute now key on the section's own `data-pf-section`
  (line 637, and the Watch-Desk count at 2522, which counted dots and now counts
  sections).
- `tests/regression/test_section_catalogue_matches_body_partials.py` — unchanged and
  passing.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | no `revealed` in `web/templates`; `intersect once` totals `31` | ✅ empty / `31` |
| 2 | contract diff (`hx-target=`, `hx-get=`, `hx-post=`, `hx-swap-oob=`, `id="tx-`, `@router.`) | ✅ **no lines** — see below |
| 3 | no `section_indicator` / `pf-section-indicator` in `web/`, `tools/`, `tests/` | ⚠️ **clean in `web/` and `tools/`**; the `tests/` hits are the absence-assertions §2.10 asked for — see below |
| 4 | DB-free selection | ✅ **98 passed** |
| 5 | DB-bound selection, Postgres up | see below |
| 6 | `ruff check` / `ruff format --check`; `pyright` advisory | ✅ all checks passed / already formatted; **pyright: 0 errors, 0 warnings** |
| 7 | hex count must not rise; report `var(--ui-` count | ✅ **0 → 0**; `var(--ui-` **134** |
| 8 | icon-only controls named; no `confirm(`/`alert(`/`hx-confirm`; no `type="number"` | ✅ — see below |
| 9 | operator walk | listed above, for the operator |

### Gate 2 — the contract diff, verbatim

```
$ git diff -U0 | grep '^[-+]' | grep -E 'hx-target=|hx-get=|hx-post=|hx-swap-oob=|id="tx-|@router\.'
$
```

No output. The trigger swap is the only `hx-*` edit in the diff; no route, target,
OOB id or `tx-` id moved.

### Gate 3 — the one place it is not literally empty

`web/` and `tools/` are clean:

```
$ grep -rn 'section_indicator\|pf-section-indicator' web/ tools/ | grep -v '^web/static/css/theme.css'
$
```

`tests/` carries seven hits, and all seven are the retirement being *asserted*:
`test_section_navigation.py` 232/248/252/268 and `test_shell_sections.py` 235/244 are
the "no `pf-section-indicator` in the page" tests §2.10 explicitly asked for, and
`test_section_navigation.py:72` is a comment naming the parked tokens. A test that
proves a string is absent has to name the string; the gate's intent — no live
surface left — is met.

### Gate 7 — CSS colour discipline

`grep -c '#[0-9a-fA-F]\{3,6\}\b' web/static/css/layout.css` → **0**, unchanged from
check 16. `grep -c 'var(--ui-'` → **134**. Every colour the rewritten blocks
introduce comes through a `--ui-*` token.

### Gate 8 — controls

- `.pf-view__search` carries visible text ("Search") beside its icon.
- The collapse button keeps its `aria-label` and its "Collapse" text span.
- The sign-out button keeps its "Sign out" text span; its collapsed-state icon
  carries `aria-label="Sign out"` (deviation 4 above).
- The sidebar glyphs are `aria-hidden` decoration beside a text label, which is
  what `pf_icon`'s default is for.
- No `confirm(`, `alert(` or `hx-confirm` added; no `type="number"` line touched
  (both greps empty).

---

## Parked: the dead `--pf-*` shell tokens

Ten tokens in the `pf` block of `config/chart_theme.json` (emitted into `theme.css`
at lines 222, 223, 226, 230 and 231–237) are no longer read by any sheet after this
change. This strand does not touch `config/`, so they stay:

```
--pf-sidebar-width-expanded      → --ui-layout-nav-width
--pf-sidebar-width-collapsed     → --ui-layout-nav-width-collapsed
--pf-bg-surface                  → --ui-background-chrome / --ui-background-secondary
--pf-accent-glow                 (focus glow; the focus ring is --ui-focus-* now)
--pf-section-indicator-width     ┐
--pf-section-indicator-right     │
--pf-section-indicator-dot-size  ├ the retired dot strip
--pf-section-indicator-dot-gap   │
--pf-section-indicator-label-bg  ┘
--pf-sticky-header-blur          ┐ the in-card blurred header, replaced by an
--pf-sticky-header-bg            ┘ opaque --ui-background-primary
```

Still live, so **not** candidates: `--pf-statusbar-height`, `--pf-bg-deepest`,
`--pf-bg-elevated`, `--pf-bg-hover` (statusbar / auth blocks in `layout.css`),
`--pf-accent-soft` (`pf_data_import.css`, `planning_desk.css`, `transactions.css`),
and the seven `--pf-palette-*` (the palette block, untouched here).

---

## Open questions

1. **The as-of and action slots are empty.** `section_as_of` and
   `section_primary_template` exist in `areas/_section.html` and are styled, but no
   area fills them yet. Which sections get an as-of date, and which get a primary
   action, is a per-area decision — worth a single pass rather than nine ad-hoc ones.

2. **`MAX_REVEAL_ROUNDS`' comment in `tools/ux_atlas.py` still says "arrives on
   `revealed`".** §2.9 says nothing else in the atlas changes, so it is left as
   written. It is stale prose, not stale behaviour; fold it into P-UX-A0t, which
   touches that file anyway.

3. **The status bar's `⌘ K jump to section` hint, and the header's `Ctrl K`.** The
   status bar renders `⌘`, the new view header renders `Ctrl K` (§2.3's markup,
   verbatim), and the JS accepts either modifier. On a Mac one of the two labels is
   wrong whichever way you look at it. The palette also now has two affordances —
   the status-bar hint and a control in every view header — and whether the shell
   wants both is an A0d/A0e question, not one to settle here.

4. **`shell.js` scrolls to the top on every view switch.** That is §2.7's
   instruction and it is right for a fragment-driven view change. It also fires on
   an `htmx:afterSwap` into `#shell-main`, which is the same event an in-place area
   refresh would raise — no such refresh exists today, but if one is added it will
   scroll the reader to the top as a side effect.

5. **The atlas covers landing sections only until P-UX-A0t.** Stated in §0 and
   repeated here because the band count in the next atlas run will drop sharply and
   that is expected, not a regression.
