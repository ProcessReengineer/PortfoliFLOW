<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0d — Component layer

**Strand:** UX A-0 · **Date:** 2026-09-23 · **Runs after:** P-UX-A0c
(`ee007e2`) · **Scope:** one new stylesheet, one new test, one `<link>`,
two empty containers, the retired statusbar shortcut, one theme token.
No `.js`, no other `components/*.css`, no route, no Python outside the
two test files.

`web/static/css/components/pf_components.css` (380 lines) transcribes the
DC-UX-D mock's component sections onto the product's generated `--ui-*`
tokens. It carries no colour literal and no `--pf-*` reference. Nothing
uses it yet: it loads first so that, as each surface migrates strand by
strand, a scoped legacy rule still out-specifies the global family and
no area changes look before its own strand touches it.

---

## OPERATOR ACTION REQUIRED

### 1. Commit

All eight code gates are green. Gate 8 is the atlas *after* run and is
yours — see §3 below.

```bash
git add web/static/css/components/pf_components.css web/templates/base.html \
        web/static/css/layout.css \
        web/templates/_partials/statusbar.html \
        web/templates/_partials/shirley_section.html \
        config/ui_theme_corporate_blue.json web/static/css/theme.css \
        tests/web/test_shell_sidebar_and_areas.py tests/web/test_pf_components.py \
        docs/reports/P-UX-A0d-report.md
git commit -m "feat(ui): component layer pf_components.css transcribed from DC-UX-D onto --ui-* tokens; a11y remap, on_accent fix, statusbar hint retired (UX A-0, P-UX-A0d)"
```

`web/templates/_partials/statusbar_oob.html` is **not** in the list — see
§Judgement calls 1.

### 2. The dev database was emptied by gate 7

The gate-7 fixtures `TRUNCATE` on teardown, as in A0b/A0c. Re-bootstrap
before using the app or running the atlas:

```bash
portfoliflow bootstrap
```

The dev Postgres container was started for this run
(`podman start portfoliflow-postgres`) and is left running.

### 3. The atlas *after* run (gate 8)

```bash
python tools/ux_atlas.py --bands 1200
```

Compare against `docs/ux/atlas/2026-09-23/`. Expected: the statusbar band
loses the `⌘ K jump to section` hint and gains nothing visible (the
activities container is `hidden`); the Cases list is unchanged; every
other area is unchanged. **One caveat on the comparison** — the baseline
carries `git_head 88739c3`, which is P-UX-A0t, one commit *before* A0c.
A0c was staged but uncommitted when this prompt opened and was committed
mid-run as `ee007e2`. A diff against that baseline therefore shows A0c's
changes as well as A0d's. A0c is role-aware navigation, and the atlas
signs in as a tenant owner, so the sidebar and command search should be
unaffected; `_admin_body.html` also changed in A0c and is not covered by
that argument. Either re-baseline first, or read any Admin-area delta as
A0c's rather than this prompt's.

---

## Verify-first

Checks 1 and 2 failed when this prompt opened: P-UX-A0c's eight files
were staged in full but never committed, so the tree was dirty and HEAD
still carried the P-UX-A0t subject. A STOP note was written. The
operator committed A0c as `ee007e2` while the remaining checks ran, the
blocker cleared, the STOP note was removed and the prompt proceeded. The
table below is the state the work actually ran against.

| # | Check | Expected | Found | |
|---|---|---|---|---|
| 1 | `git status --porcelain` | empty | empty (after `ee007e2`; was 8 staged A0c files) | ✅ |
| 2 | `git log --oneline -1` | ends `(UX A-0, P-UX-A0c)` | `ee007e2 feat(shell): role-aware section catalogue … (UX A-0, P-UX-A0c)` | ✅ |
| 3 | `pf_components.css`, `P-UX-A0d-report.md` | neither exists | neither existed | ✅ |
| 4 | `shared.css`: 601 / 591 / 32 | 601; 591; 32 | 601 ✅; 591 ✅; 32 ✅ — counted two different ways, see below | ✅ |
| 5 | `wc -l` theme/layout/base/cases | 409 / 813 / 289 / 1024 | 409 / 813 / 289 / 1024 | ✅ |
| 6 | distinct `--ui-*`; container in `layout.css` | ≥ 79; no hit | 79; no hit | ✅ |
| 7 | `.pf-cases .pf-btn` lines | 42, 53, 58, 65 | 42, 53, 58, 65 | ✅ |
| 8 | `pf-btn*` uses in templates | 14 / 5, nothing else | 14 / 5; `cases_detail.html` + four `cases_composer_*.html` | ✅ |
| 9 | `pf-statusbar__shortcut` sites | template 33–36; layout 701/708/717; two assertions | exactly that | ✅ |
| 10 | `on_accent` | three hits, line 17 | line 17 in all three; `corporate_blue` `#000000`, `light` `#FFFFFF` | ✅ |
| 11 | `base.html` 33, 40 | layout + first component link | 33 `layout.css`, 40 `benchmarks_attribution.css` | ✅ |
| 12 | `#chat-history`; statusbar lengths | 54; 51 / 10 | 54; 51 / 10 | ✅ |
| 13 | baseline atlas | present; `88739c3` | present; `88739c3` | ✅ |

**Count note on check 4.** The prompt spells both greps with `-c`, which
counts matching *lines*, but the two expected numbers come from different
forms. `var(--pf-` is 281 lines / **591 occurrences**; the six-digit hex
pattern is **32 lines** / 38 occurrences. Both expectations reproduce,
each under one form, and the file is the right one
(`md5 5f90bcc09bfa27d461d1bba012f58a06`, 601 lines). A re-issue may want
`grep -o … | wc -l` for the first and `grep -c -E` for the second so the
two read alike.

**Count drifts elsewhere (anchors intact, none blocking).**

* §5.2 gives the `#chat-history` div as "line 54–68"; it opens at 54 and
  closes at 63. The insert went after 63.
* §6 gives the `layout.css` rules as 701–723 and "~23 lines"; the three
  rules plus their trailing blank are 701–721, so 21 lines came out.
  722 is the start of the AGPL comment and was not touched.
* §8.1 asks for the `<link>` on line 40. It sits at line 43 because a
  three-line explanatory comment precedes it. The ordering constraint —
  after `layout.css`, before every `components/*.css` — holds and is what
  the new test pins.

---

## Transcription ledger

The source has 227 `pf-*` classes. 133 were taken, 94 left out, and 3
classes appear in the file that are not in the source (below).

| Source section | Lines | Taken | Left out of that range |
|---|---|---|---|
| Kbd | 121 | `pf-kbd` | — |
| The one button family | 123–134 | `pf-btn` + `--primary`/`--quiet`/`--icon`/`--sm` | — (`--danger` comes from 384–385, with the action bar) |
| Chart frame | 158–166, 595–596 | `pf-chart`, `__title`, `__legend`, `__key`, `__plot` | `pf-triplet`, `pf-placeholder`, the three container blocks |
| Crumb | 232–238 | all four | — |
| Functional state | 239–248 | all eight | — (plus `--dormant`, added) |
| Notice and message | 249–262 | `pf-note` family, `pf-neg` | — |
| Disclosure | 263–271 | `pf-more` | — |
| Table | 272–288 | all | — |
| Menu | 289–297 | all | — |
| Slot panel | 298–322 | `pf-panel` family, `pf-facts` | `pf-lenses`, `pf-lens__title`, `pf-delta*` |
| Form | 323–357 | `pf-form`, `__main`, `pf-field` + 3 modifiers, `pf-label`, `pf-optional`, `pf-input`/`--num`, `pf-select`, `pf-textarea`, `pf-hint`, `pf-check` | `pf-block__title`, `pf-grid`, `pf-seg`, `pf-context`, `pf-group`, two container blocks |
| Summary rail | 358–380 | `pf-rail-sum`, `__title`, `pf-sum`, `pf-leg` | `pf-choice*`, one container block |
| Action bar | 381–388 | `pf-actionbar`, `__hint`, `pf-btn--danger`, `pf-btn[disabled]`, `pf-btn--primary[disabled]` | — |
| Flow list, Filters, Empty | 389–409 | `pf-flows`, `pf-flow*`, `pf-filters*`, `pf-empty*` | `pf-view__notice`, `pf-view__stamp` |
| As-of, Figures, Sources, Activity, Skeleton | 416–465 | `pf-asof*`, `pf-figures`, `pf-sources`, `pf-activity*`, `pf-spin`, `pf-skel`/`--plot`, `pf-sr` | `pf-fresh*`, `pf-hero*`, `pf-job*`, `pf-progress*`, `@keyframes pf-slide`, one container block |
| Setup, Switch/settings, Number entry | 466–504 | `pf-setup*`, `pf-step*`, `pf-settings*`, `pf-setting*`, `pf-switch`, `pf-read`, `.pf-input.is-unclear` | `pf-ask` |
| Accessible mode | 505–528 | see §3 below | `pf-chat__tools` and `pf-status` lines |
| Tabs, Toolbar, Sortable header | 534–559 | `pf-tabs`, `pf-tab`, `pf-toolbar*`, `th[aria-sort]`, `td.pf-pos`, `pf-table__chev`, `pf-panel__chart`, `pf-scroll-x` | — |
| Reading width, Action groups, Invalid field, Self marker | 565–588 | `pf-narrow`, `pf-acts`, `pf-act`, `__row`, `__warn`, `__text`, `aria-invalid`, `pf-self` | `pf-act__spacer`, two container blocks |

**Left out wholesale** (shell and Shirley — `layout.css`'s or A0e's), so
A0e and A-4 can find them: `:root` 1–60, `pf-shell`, `pf-icon`, `pf-nav*`,
`pf-main`, `pf-view*`, `pf-search*`, `pf-skip`, `pf-side`, `pf-rail*`,
`pf-presence`, `pf-dockhost`, `pf-chat*`, `pf-msg*`, `pf-preview*`,
`pf-stage*`, `pf-status*`, `pf-inv*`, `pf-kv*`, `pf-triplet`,
`pf-charts-grid`, `pf-kpis`, `pf-lenses`, `pf-lens__title`, `pf-delta*`,
`pf-hero*`, `pf-context`, `pf-choice*`, `pf-progress*`, `pf-job*`,
`pf-fresh*`, `pf-placeholder`, `pf-seg`, `pf-grid`, `pf-group`,
`pf-block__title`, `pf-ask`, and every `@container` block.

**Three classes in the file that are not in the source:**

1. `pf-state--dormant` — the one addition the prompt asks for (Phase A,
   Q-UX-A-11), marked as such at its site.
2. `pf-sidebar__item` and `pf-sidebar__section` — the product's names for
   the mock's `pf-nav__item` / `pf-nav__section` in the accessible-mode
   no-underline rule (source line 518). Transcribed literally the rule
   would target nothing and every sidebar entry would be underlined in
   accessible mode, which is the bug that rule exists to prevent.

---

## Token mapping — verifications

**The background ladder verifies by name *and* by value**, so the
straight name mapping holds and no by-value reordering was needed:

| Mock | Value | Product | Value |
|---|---|---|---|
| `--pf-bg-0` | `#050505` | `--ui-background-chrome` | `#050505` |
| `--pf-bg-1` | `#0A0A0A` | `--ui-background-primary` | `#0A0A0A` |
| `--pf-bg-2` | `#141414` | `--ui-background-secondary` | `#141414` |
| `--pf-bg-3` | `#1A1A1A` | `--ui-background-section` | `#1A1A1A` |

The four semantic colours verify by value too (`#4CAF50` / `#E8304A` /
`#4A9BD9` / `#FFA726` → `--ui-semantic-positive` / `-negative` / `-info` /
`-warn`), as do the text ladder, borders, accent, spacing, radii, motion
and the four control tokens.

**The four `--chart-*` names used**, all declared in `theme.css`:
`--chart-colours-plot-area` (the frame background, so the frame and the
Plotly paper are always the same surface — record §2.12),
`--chart-colours-text` (`.pf-chart__plot text` fill). No gridline rule
was added: the chart theme already publishes `--chart-colours-grid` as
`#00000000`. (Two names, not four — `--chart-colours-background` and
`--chart-colours-grid` were checked and deliberately not referenced.)

**Hex literals that were not inside the mock's `:root`.** The prompt
states all 32 live in `:root` or the a11y/brand blocks; five do not, and
each needed a decision:

| Source | Literal | Became |
|---|---|---|
| 254, 261, 296, 384, 582 | `#FF6B6B` | `var(--ui-semantic-error)` — exact value match |
| 595 / 596 | `#1A1A1A` / `#D4D4D4` | `var(--chart-colours-plot-area)` / `var(--chart-colours-text)` — exact value matches |
| 474, 496 | `#000` (ink on a filled green mark) | `var(--ui-background-chrome)` — see §Deliberate deviations 3 |
| 337 | `#3A3A3A` (input hover border) | `color-mix(…)` — see §Deliberate deviations 4 |
| 384, 385 | `#5A2A2E`, `#2A1517` (danger button) | `color-mix(…)` — see §Deliberate deviations 4 |

`--ui-button-fg` is referenced nowhere in the new file:
`.pf-btn--primary` reads `--ui-accent-on-accent`, and the new test pins
that.

---

## §4 — `on_accent` for corporate blue

`config/ui_theme_corporate_blue.json:17` `#000000` → `#FFFFFF`, then
`python scripts/generate_theme_artifacts.py`. `theme.css` shows exactly
one changed line — `--ui-accent-on-accent` under
`:root[data-theme="corporate_blue"]` — and stays at 409 lines.
`generate_theme_artifacts.py --check` passes (the script has a check
mode; no second-run diff was needed). No other generated artefact moved.

**The change is invisible today.** `--ui-accent-on-accent` is read by
exactly one rule in the whole tree — `.pf-btn--primary`, in the new file
— and no template uses `pf-btn--primary` outside `.pf-cases`, where
`cases.css` wins on specificity and reads `--ui-button-fg`. The fix is
therefore correct-ahead-of-use, not a visual change.

---

## §5 — the two empty containers

1. `statusbar.html`: `<div class="pf-activity" id="pf-activities" hidden></div>`
   in `pf-statusbar__group--right`, immediately before the build stamp.
   `grep -c hidden` rose 1 → 2 as specified.
2. `shirley_section.html`: `<ul class="pf-sources" id="shirley-sources" hidden></ul>`
   directly after the `#chat-history` div (which closes at 63, not 68).

Neither has content, script or route. Both carry a comment naming the
prompt so A0e and the later job-feed strand know what they are.

---

## §6 — the shortcut hint

The view header's search field is the one shortcut affordance (record
§2.1.7: visible field in every view header, tooltip carries the shortcut,
written "Ctrl K"). The statusbar hint duplicated it with `⌘`, wrong on
every non-Mac keyboard.

* `statusbar.html` 33–37 removed (5 lines).
* `layout.css` 701–721 removed — 21 deletions, 0 insertions, nothing else
  in the file touched.
* `tests/web/test_shell_sidebar_and_areas.py:387` inverted to `not in`,
  with the reason in the comment and four sentences added to the
  docstring. `test_statusbar.py:246` already asserted absence and stands.
* The `--pf-section-indicator-*` and statusbar tokens in
  `chart_theme.json`'s `pf` block were left alone (parked cleanup).

**Container queries stay out of this prompt.** `layout.css` declares no
container, so every `@container` block in the source would be inert. The
new test pins their absence.

---

## §7 — Cases `pf-btn`: verified, left alone

Read and confirmed: `cases.css` 42, 53, 58, 65 each carry a `.pf-cases`
prefix and each shares its rule with a `.pf-cases__new-btn*` selector on
the next line. `.pf-cases .pf-btn` is specificity (0,2,0) against the
global `.pf-btn`'s (0,1,0), so Cases keeps its current look on
specificity alone — the load order only reinforces it. Nothing was
renamed or deleted; gate 6 confirms this prompt adds no `pf-btn` use. The
cleanup is A-3's, and it needs a seeded case-detail scene first (§Flags).

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/web/test_pf_components.py tests/regression -q` | ✅ 295 passed, 3:36 |
| 2 | `ruff check .` · `ruff format --check` · `generate_theme_artifacts.py --check` | ✅ all clean |
| 3 | `git diff --name-only` | ✅ exactly the expected set, minus `statusbar_oob.html` (§Judgement calls 1). No `.js`, no other `components/*.css`, no other template |
| 4 | hex / `var(--pf-` / `@container` in the new file | ✅ 0 |
| 5 | `git diff --stat` theme + layout | ✅ theme.css 1 line changed (409 held); layout.css 21 deletions, 0 insertions |
| 6 | `pf-btn*` uses in templates | ✅ unchanged: 14 / 5 |
| 7 | the §8.3 DB-bound selection | ✅ 332 passed, 11:23 |
| 8 | atlas *after* | operator — §OPERATOR ACTION 3 |

Gate 1 needed the dev Postgres up: `tests/regression` contains one
DB-bound test (`test_super_admin_audit_schema.py`), which errors rather
than skips when the server is unreachable. Gate 7's 332 are the 176
DB-bound web tests of the §8.3 selection plus the 156 regression tests;
the only edited test in it is the one line and docstring of §6.

### Why gate 8 should show nothing (checked, not assumed)

Of the 136 classes the file defines, **seven** already appear in a
template, and each is inert:

| Class | Where | Why nothing changes |
|---|---|---|
| `pf-sources` | the container this prompt adds | `hidden`, and guarded by `.pf-sources[hidden]` |
| `pf-activity` | the container this prompt adds | `hidden`; the rule sets only `position`, so the UA rule holds |
| `pf-btn`, `pf-btn--primary` | `cases_detail.html` + four composers | always inside `.pf-cases`, where `cases.css` wins on specificity (0,2,0) vs (0,1,0) |
| `pf-shell` | `base.html` | all eight of the file's `.pf-shell` rules are gated on `[data-a11y="on"]`, which nothing writes |
| `pf-sidebar__item`, `pf-sidebar__section` | `sidebar.html` | only inside those same `[data-a11y="on"]` rules |

Every other class in the vocabulary is currently unused, so the file is
loaded and dormant.

### Gate 2 detail

`ruff format` reformatted `tests/web/test_pf_components.py` once on
first write; `--check` is clean after. `ruff check .` passes tree-wide.

### The new test — `tests/web/test_pf_components.py`

139 test ids, DB-free (regexes over file sources; no app, no browser),
0.8 s. It pins: the file exists and `base.html` links it after
`layout.css` and before every other `components/*.css`; zero hex, zero
`rgb(`/`hsl(`, zero `var(--pf-`; every `--ui-*`/`--chart-*` it reads is
declared in `theme.css` (both files parsed, unknown names fail with the
list); each of 130 named classes from the §1 table is defined (the list is
written out in the test, so it does not depend on `~/DC-UX-D`);
`.pf-state--dormant`; `.pf-shell[data-a11y="on"]` with the
`data-sidebar-collapsed` guard and six accessible tokens;
`--ui-accent-on-accent` present and `--ui-button-fg` absent; no
`@container`; the two `[hidden]` guards (below); and `layout.css` free
of `pf-statusbar__shortcut`.

---

## Judgement calls

1. **`statusbar_oob.html` was not edited.** It is ten lines that set
   `statusbar_oob = True` and `{% include "_partials/statusbar.html" %}`,
   so it re-renders the *whole* bar, right group included. The activities
   container is mirrored automatically; editing the file would have been
   a no-op at best and a divergence at worst. §5.1's conditional
   therefore does not apply, and the file is not in the commit list.

2. **`.pf-check` and `.pf-field--half` moved to the Form section.** The
   source has them at 379 (summary rail) and 583 (invalid field); the §1
   table assigns both to the Form row. They are form controls, so they
   sit with the other field modifiers. Everything else keeps the source's
   section and order.

3. **`.pf-select` was kept although the §1 class lists do not name it.**
   It shares four rules with `.pf-input` and `.pf-textarea` (335, 337,
   338, 572) and two more with `aria-invalid` and `.pf-filters`.
   Dropping it would have meant splitting those rules.

4. **`.pf-choice input` was dropped from the shared `accent-color` rule**
   (source 377) because `pf-choice*` is on the leave-out list;
   `.pf-check input` kept it.

5. **`.pf-act__spacer` was dropped.** Source 576 is an empty ruleset
   whose only meaning comes from the container query at 577, which this
   prompt leaves out. An empty rule carrying no behaviour is noise; the
   class returns with the container declaration.

6. **`@keyframes pf-slide` was dropped** with `pf-progress`, its only
   consumer. `pf-spin` and `pf-pulse` were kept — both animate classes
   that are in.

---

## Deliberate deviations

1. **The accessible mode's icon-label rules were dropped.** §3 lists
   "icon-label" among the rules to keep and, in the same sentence, says
   to drop the `pf-chat__tools` lines. Source 520–521 *are* the
   icon-label rules and they are `pf-chat__tools`-scoped, so the two
   instructions collide. The explicit drop won: unscoping them would
   have made every icon button in the product grow a text label in
   accessible mode, which is a redesign, not a transcription. The
   behaviour belongs with the chat partial in A0e.

2. **The accessible mode widens `--ui-layout-nav-width-collapsed` too,
   not only the guard.** `layout.css:34` switches the collapsed grid on
   `--ui-layout-nav-width-collapsed` directly rather than redefining the
   expanded name, so the guard §3 specifies — which sets
   `--ui-layout-nav-width` — is inert for the grid on its own. Both are
   written: the remap in the main block makes the collapsed nav actually
   reach 64 px, and the guard (kept verbatim, and pinned by the test)
   covers any consumer that reads the expanded name while collapsed.

3. **`#000` became `var(--ui-background-chrome)`, not
   `--ui-accent-on-accent`.** Both `#000` uses are ink on a filled
   `--ui-semantic-positive` mark (the done step, the checked switch), not
   ink on the accent. `--ui-background-chrome` gives black on green in
   dark and corporate blue and white on dark green in light — correct
   contrast in all three. `--ui-accent-on-accent` would have gone white
   on `#4CAF50` in corporate blue after §4, at roughly 2.8:1.

4. **Three literals became `color-mix()`.** The product has no token at
   these values and §4's one-changed-line gate rules out adding one here:

   | Source | Literal | Written as | Resolves to (dark) |
   |---|---|---|---|
   | 337 | `#3A3A3A` | `color-mix(in srgb, var(--ui-border-default) 80%, var(--ui-text-tertiary))` | `#3E3E3E` |
   | 384 | `#5A2A2E` | `color-mix(in srgb, var(--ui-semantic-error) 30%, var(--ui-background-secondary))` | `#5A2E2E` |
   | 385 | `#2A1517` | `color-mix(in srgb, var(--ui-semantic-error) 10%, var(--ui-background-secondary))` | `#2C1D1D` |

   Each is within a few points per channel of the mock in dark mode, and
   unlike a frozen literal each derives correctly in light mode, where
   the error colour is `#C62828` on a light surface. `color-mix` is
   Baseline-2023 and needs no fallback for the product's target browsers.

5. **Two `rgba(0, 0, 0, .6)` drop shadows survive as literals** — the
   menu and the activity panel (source 293, 448). They are shadows, not
   theme colours, and the gates as specified (hex, `rgb(`, `hsl(`) do not
   catch `rgba(`. Stating it rather than smuggling it: a `--ui-shadow-*`
   token would be the clean fix, and it is on the A0d2 list below.

6. **`.pf-narrow` reads `var(--ui-layout-reading-width)`** rather than
   keeping `960px`. The token exists and holds exactly that value, so
   this is a token reference, not a geometry literal.

7. **`@media (prefers-contrast: more)` reaches the default theme only.**
   The rule remaps `:root`, and `:root[data-theme="light"]` /
   `[data-theme="corporate_blue"]` out-specify a bare `:root`, so the two
   named themes keep their own ladder. Transcribed faithfully and
   commented at its site; making it theme-complete means three blocks,
   which is a token-generation decision, not this prompt's.

8. **`.pf-sources` gained a `[hidden]` guard that the source does not
   have.** An author `display` declaration beats the user-agent's
   `[hidden] { display: none }` whatever the specificity — the trap
   `layout.css` already documents for `.pf-section`. `.pf-sources` sets
   `display: flex` with a `border-top`, so §5.2's empty hidden container
   would have drawn a stray rule under the Shirley chat on every page —
   a visible change, and a gate-8 failure. `.pf-sources[hidden] {
   display: none; }` is the same one-line fix the source already applies
   to `.pf-activity__panel`. `.pf-activity` needs none: it sets only
   `position`, so the UA rule holds. A test pins both, and pins that
   `.pf-activity` stays display-free.

---

## §7 Flags

1. **`.pf-main` declares no container.** `container: view / inline-size`
   must land before any container query can, and the source has twelve
   of them waiting: the search collapse (view header), the chart triplet
   (two) and the charts grid (A-4), the stage (A0e), and the
   narrow-width collapses for `pf-form`, `pf-field`/`--wide`,
   `pf-field--half`, `pf-lenses`, `pf-rail-sum`, `pf-asof` and
   `pf-act__spacer`. **Re-check the sticky view header when
   it lands** — `container-type: inline-size` establishes containment and
   can change how a `position: sticky` descendant resolves.
2. **`--ui-button-fg` is still live in 24 places across 8 files**, more
   than the prompt's flag anticipated: `base.css` 100/208,
   `investments.css` (12), `transactions.css` (4),
   `benchmarks_attribution.css`, `tables.css`, `saa.css`,
   `watch_desk.css`, and `cases.css:61` for
   `.pf-cases__new-btn--primary` (line 61, not 62). A0d2 and A-3 retire
   it; until then corporate blue's `button.fg` and `accent.on_accent`
   agree (`#FFFFFF`), so the two tokens do not disagree on screen today.
3. **`_auth_base.html` does not link the component file.** Login keeps
   its own vocabulary until A-5.
4. **`data-a11y` has no writer.** The remap is complete and inert; the
   stored preference is a later prompt (record §2.11, Q-UX-D-13). The
   A0s checklist toggles the attribute in devtools.
5. **`--ui-accessible-control-row-pad` does not exist.** `theme.css`
   publishes accessible variants for the five font scales, two text
   colours, the default border, three control heights, motion, the two
   nav widths and the dock — but not the row pad, and not
   `border-soft`. The a11y block therefore restates the source's `14px`
   for the row pad and omits the `border-soft` remap entirely. Adding
   the two tokens to `config/ui_theme*.json` would have broken §4's
   one-changed-line gate on `theme.css`, so it is deferred; it is a
   two-line JSON change plus a regeneration whenever the next prompt
   touches the theme.
6. **The Cases `pf-btn` collision is unresolved by design** (§7). Before
   A-3 touches it, `atlas-scenes.json` needs a seeded case-detail scene:
   the baseline shows the Cases list in its empty state only, so the 14
   `pf-btn` uses in `cases_detail.html` and the four composers have **no
   before image** at all.
7. **A `--ui-shadow-*` token is missing** — see §Deliberate deviations 5.
8. *Carried from the baseline run, not from this prompt:* the sticky
   statusbar overlaps the sidebar footer and cuts off "Sign out" (A0e,
   where the grid is touched anyway); Back Office is `suspect` because a
   benchmarks-attribution loader stays visible on the empty DB.

---

## Handover — DC-UX-A2

> **P-UX-A0d done.** `pf_components.css` (380 lines) is the one component
> vocabulary, transcribed from DC-UX-D onto `--ui-*` with no colour
> literal; it loads before every legacy sheet and nothing uses it yet, so
> no surface changes look. The statusbar shortcut hint is retired in
> favour of the view header's search field, corporate blue's `on_accent`
> is white, and two empty containers are in place for A0e.
>
> **Next prompt needs:** `.pf-main` to declare `container: view /
> inline-size` before any container query lands (re-check the sticky view
> header then), and a seeded case-detail scene in `atlas-scenes.json`
> before A-3 touches the Cases buttons.

Attached: the post-A0d atlas zip and the statusbar band image.
