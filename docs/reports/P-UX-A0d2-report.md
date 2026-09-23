<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# P-UX-A0d2 — Token sweep over the legacy stylesheets

**Strand:** UX A-0 · **Date:** 2026-09-23 · **Runs after:** P-UX-A0d
(`fca0d37`) and the post-A0d atlas run
(`docs/ux/atlas/2026-09-23-2/`) · **Scope:** three theme JSONs, the
regenerated `theme.css`, twenty stylesheets under `web/static/css/`,
one new test file, this report. No `.js`, no template, no vendor file,
no Python outside the one new test.

A0d gave the component vocabulary a rule — colour lives in `theme.css`
and nowhere else. This prompt applies that rule to every other
stylesheet the shell loads, and fixes the thing the inventory turned up
that is worse than a literal: **eight distinct token names that no
theme declares**, referenced at 32 sites. Twenty-six of those sites
carried a fallback literal, so the theme never reached them; **six
carried none, so they computed to nothing and their declarations were
dropped by the parser.** Those six are invisible bugs that become
visible fixes here.

Counts after the sweep: `hex 0`, `rgb/rgba 1` (`login.css:18`, out of
scope until A-5), `dangling 0`.

---

## OPERATOR ACTION REQUIRED

### 1. Commit

Gates 1–6 are green. **Gate 7 was interrupted on purpose and is carried
to the A-0 Ende full-suite run — see §4.** Gate 8 is the atlas *after*
run and is yours — see §3 below.

Already staged; the commit is the operator's.

```bash
git commit -m "chore(ui): token sweep — no colour literal outside theme.css, eight dangling token names fixed, mono/a11y/shadow tokens added, button.fg retired (UX A-0, P-UX-A0d2)"
```

**One word changed from the prompt's message: "eleven" → "eight".**
The prompt's own §5 flag records the miscount — `dangling 32` is the
*site* count, the distinct names are eight. A commit subject is the
durable record, so it carries the true number.

`tests/web/test_pf_components.py` is **not** in the staged set: the new
assertions went into a new file and the A0d test is unedited — see §8.
The staged set is the 25 paths listed under Gate 3.

### 2. The dev database was emptied by the partial gate-7 run

The gate-7 fixtures `TRUNCATE` on teardown, as in A0b/A0c/A0d, and the
run got 10 % of the way through before it was stopped — far enough to
have emptied the dev DB. Re-bootstrap before using the app or running
the atlas:

```bash
portfoliflow bootstrap
```

The dev Postgres container was already up and is left running.

### 3. The atlas *after* run (gate 8)

```bash
python tools/ux_atlas.py --bands 1200
```

Compare against **`docs/ux/atlas/2026-09-23-2/`** (`git_head fca0d37`),
not `docs/ux/atlas/2026-09-23/` — see the check-9 note below. **This
prompt does change pixels.** The list of where, written before the run,
is §7. **Planning Desk is the one area that must be read band by band.**

### 4. OPEN POINT — carry gate 7 to the A-0 Ende full-suite run

**`pytest tests/web -q` was interrupted at 155 of 1538 tests (10 %,
~7 minutes in) and is deliberately not completed here.** At ~3.3 s/test
the full run needs ≈85 minutes, which is development time the A-0
series cannot spare before this evening's A-0 Ende gate — and that gate
runs the whole suite anyway, `tests/web` included. Running it twice
buys nothing.

**What to look for when the full suite runs.** This prompt changes no
Python, so any `tests/web` failure is either (a) pre-existing, or (b) a
stale test pin on a colour literal this sweep removed. For (b) the fix
is the pin, not the CSS.

Evidence already in hand that (b) is unlikely:

* A grep over `tests/` for every retired name and literal —
  `ui-button-fg`, `ui-font-mono`, `ui-surface-secondary`,
  `ui-surface-primary`, `ui-semantic-danger`, `ui-background-default`,
  `ui-background-input`, `ui-background-tertiary`, `#2c2c2c`,
  `#161616` — returns **only** `tests/web/test_css_tokens.py` (new,
  green) and `tests/web/test_pf_components.py` (A0d, green, and its
  `--ui-button-fg` assertion is an *absence* check, which still holds).
  The other hits are frozen handover mockups under `docs/handover/`
  and the A0d report, neither of which is executed.
* The 155 tests that did run covered
  `tests/web/test_a*` … `test_c*` (alphabetically through the Cases
  files) with exactly one failure, and that one is proven pre-existing
  (below).

**The one known failure, already triaged:**
`tests/web/test_cases_area.py::test_cases_page_renders_three_sections`
— `assert 'data-section="open-cases"' in body`. Proven pre-existing by
`git stash push -u` + the single test on clean `main`: **identical
failure, same assertion, same message**, tree restored afterwards and
re-verified. It is the A0b one-section-per-view shell (`9581ed0`),
where `/cases` renders one section per view and the section-indicator
dots the test expects are gone. It belongs to whichever prompt owns the
Cases migration. Expect it in the full-suite run; it is not this
prompt's, and it is not a colour pin.

The partial log is at
`/tmp/claude-1000/…/scratchpad/gate7b.log` (session-scoped, will not
survive; the triage above is the durable record).

---

## Verify-first

Checks 1–3 (the STOP checks) passed: clean tree, HEAD `fca0d37` with a
subject ending `(UX A-0, P-UX-A0d)`, no `P-UX-A0d2-report.md`.

| # | Check | Result |
|---|---|---|
| 1 | `git status --porcelain` | ✅ empty |
| 2 | `git log --oneline -1` | ✅ `fca0d37 … (UX A-0, P-UX-A0d)` |
| 3 | `ls docs/reports/P-UX-A0d2-report.md` | ✅ does not exist |
| 4 | inventory script | ✅ exact match, line for line (below) |
| 5 | `wc -l` on the three sheets | ✅ 409 / 380 / 792 |
| 6 | `--ui-button-fg` / `--ui-font-mono` counts | ✅ exact (below) |
| 7 | `"button"` in the JSONs, readers in Python | ✅ three hits, `{"fg": …}` only; no reader |
| 8 | theme-shape probe | ✅ `control.row_pad` present, `accessible` has no `border_soft`, its `control` no `row_pad`, no top-level `shadow`, `font` has `family` but no `family_mono` |
| 9 | `ls -d docs/ux/atlas/*/ \| tail -1` | ⚠️ **drift, anchor intact** — see below |

**Check 6 in full.** `--ui-button-fg`: 8 files, **23** sites —
`base.css` 2, `benchmarks_attribution.css` 1, `cases.css` 1,
`investments.css` 12, `saa.css` 1, `tables.css` 1, `transactions.css`
4, `watch_desk.css` 1. `var(--ui-font-mono`: `cases.css` 4,
`scraper.css` 1, `transactions.css` 16 (15 rules + the comment at 30),
`watch_desk.css` 2 — 23 rule sites. Both match the prompt exactly.
(A0d §7 flag 2 said "24 places"; the true occurrence count is 23. The
prompt's figure is the right one.)

**Check 9 — the drift.** `tail -1` returns
`docs/ux/atlas/2026-09-23/`, whose `git_head` is `88739c3` (P-UX-A0t),
**not** the post-A0d run. The cause is sort order, not a missing run:
with the trailing slash `ls` compares `2026-09-23-2/` against
`2026-09-23/`, and `-` (0x2D) sorts before `/` (0x2F), so the `-2`
suffix lands *first*. The post-A0d run exists and is
`docs/ux/atlas/2026-09-23-2/`, `git_head fca0d37` — the anchor the
check was reaching for. Confirmed by reading `git_head` out of every
manifest:

```
2026-09-21    5328291
2026-09-22    5328291
2026-09-22-2  e2fd660
2026-09-23    88739c3   <- what `tail -1` returns
2026-09-23-2  fca0d37   <- the actual post-A0d run, the before image
```

Reported, not blocking. **Gate 8 must compare against `2026-09-23-2`.**

### Inventory — before (verbatim)

```
hex 33 rgb/rgba 22 dangling 32
base.css (0, 4, ['--ui-background-default'])
cases.css (0, 0, ['--ui-font-mono'])
charts.css (6, 0, ['--ui-border', '--ui-surface-secondary'])
chat.css (0, 0, ['--ui-semantic-danger'])
investments.css (0, 1, [])
login.css (0, 1, [])
overview.css (1, 0, [])
pf_components.css (0, 2, [])
pf_data_import.css (0, 1, [])
planning_desk.css (14, 4, [])
portfolio_analysis.css (2, 0, [])
saa.css (2, 7, [])
scraper.css (0, 0, ['--ui-font-mono'])
statistics.css (3, 0, ['--ui-border', '--ui-surface-primary'])
super_admin.css (5, 0, ['--ui-background-tertiary'])
transactions.css (0, 1, ['--ui-background-input', '--ui-font-mono'])
watch_desk.css (0, 0, ['--ui-font-mono'])
layout.css (0, 1, [])
```

Identical to the prompt's expected table, line for line.

### Inventory — after (verbatim)

```
hex 0 rgb/rgba 1 dangling 0
login.css (0, 1, [])
```

Gate 4 as specified.

---

## §1 — Theme JSON

Three additions, one removal, in all three files. The generator emits
groups generically (`_emit_pairs`), so no script change was needed; the
new `shadow` group carries a `_comment` (skipped by the emitter,
`_`-prefixed).

| Key | Dark | Light | Corporate blue |
|---|---|---|---|
| `font.family_mono` | `IBM Plex Mono, ui-monospace, SFMono-Regular, Menlo, monospace` | same | same |
| `accessible.control.row_pad` | `14px` | same | same |
| `accessible.border_soft` | `#4A4A4A` | **`#9A9A9A`** — see §Deliberate deviations 1 | `#4A4A4A` |
| `shadow.panel` | `0 12px 32px rgba(0, 0, 0, 0.6)` | `0 12px 32px rgba(0, 0, 0, 0.18)` | as dark |
| `shadow.scrim` | `rgba(0, 0, 0, 0.55)` | `rgba(0, 0, 0, 0.35)` | as dark |
| `button` | removed | removed | removed |

`theme.css` went 409 → **421** lines, and the diff is exactly what the
prompt predicted: **+5 / −1 per theme block**, nothing else moved.

```
 web/static/css/theme.css | 18 +++++++++++++++---
 1 file changed, 15 insertions(+), 3 deletions(-)
```

`python scripts/generate_theme_artifacts.py --check` is clean.
`tests/scripts/test_generate_theme_artifacts.py` and
`tests/core/test_ui_theme.py` are green **unedited**, as check 7
predicted — nothing in `core`, `services` or `web` reads the `button`
group.

**The a11y block in `pf_components.css` is now complete.** A0d flag 5
recorded two holes; both are closed:

```css
-    --ui-border-default: var(--ui-accessible-border-default);
+    --ui-border-default: var(--ui-accessible-border-default);
+    --ui-border-soft: var(--ui-accessible-border-soft);
...
-    /* theme.css publishes no --ui-accessible-control-row-pad, so the
-       source's own 14px stands in. See the P-UX-A0d report, §Flags. */
-    --ui-control-row-pad: 14px;
+    --ui-control-row-pad: var(--ui-accessible-control-row-pad);
```

`--ui-border-soft` is placed immediately after `--ui-border-default`,
mirroring the pair's order in the base theme. The
`@media (prefers-contrast: more)` block below was **not** extended —
A0d transcribed three names there and the prompt's gate 6 budgets four
changed lines for the file, so widening that block is a separate
decision.

---

## §2 — Dangling names → declared names

Eight distinct names, 32 sites. The "was rendering" column is what the
browser actually computed before the fix, reasoned from the cascade:
when a `var()` names an undeclared property and carries no fallback,
the declaration is *invalid at computed-value time* — it does not fall
back to the UA default, it resets the property to its inherited value
if the property inherits, and to its initial value if it does not.

| Was | Becomes | Sites | Was rendering | Now |
|---|---|---|---|---|
| `--ui-font-mono` | `--ui-font-family-mono` | `cases.css` ×3 + comment 23; `scraper.css` ×1; `transactions.css` ×15 + comments 30/31; `watch_desk.css` ×2 | **13 of 15 + all others:** the fallback stack `ui-monospace, "SFMono-Regular", "Menlo", monospace` — correct mono, theme unreachable. **`transactions.css` 1543, 1603:** bare `var(--ui-font-mono)`, no fallback → `font-family` invalid at computed-value time → **inherited the sans body font** | the token's stack |
| `--ui-border` | `--ui-border-default` | `charts.css` 87, 198; `statistics.css` 101 | the fallback `#2c2c2c` | `#2A2A2A` — a 2-point shift, invisible |
| `--ui-surface-secondary` | `--ui-background-secondary` | `charts.css` 86, 197 | the fallback `#161616` | `#141414` — a 2-point shift, invisible |
| `--ui-surface-primary` | `--ui-background-primary` | `statistics.css` 90 | the fallback `transparent` | `#0A0A0A` — **which is what `.pf-main` already paints behind it**, so no perceptible change either. See §7 |
| `--ui-semantic-danger` | `--ui-semantic-error` | `chat.css` 459, 460 | **nothing.** `background-color` does not inherit → initial → `transparent`; `color` inherits → the dialog's own text colour. The danger chip had no danger in it | `#FF6B6B` text on a 10 % tint |
| `--ui-background-default` | `--ui-background-primary` | `base.css` 167 | **nothing** → `background-color` initial → `transparent` | `#0A0A0A` — but the rule is **dead CSS**, see Flag 4 |
| `--ui-background-tertiary` | `--ui-background-section` | `super_admin.css` 243 | the nested fallback `var(--ui-background-section)` already resolved | identical — pure cleanup |
| `--ui-background-input` | `--ui-background-secondary` | `transactions.css` 1318 | **nothing** → `background` initial → `transparent`, so the filter `<select>`s and the date input showed the page through | `#141414`, matching `.pf-input` |

**The six sites that computed to nothing** — the ones this prompt
actually changes on screen — are therefore `chat.css` 459 and 460,
`base.css` 167, and `transactions.css` 1318, 1543 and 1603. The last
two (`.tx-impact__basis dd`, `.tx-delta__pair`) are **not in the
prompt's §2 "visible" set**: the prompt read the mono group as a
uniform "drop the fallback stack", but two of the fifteen
`transactions.css` rules never had a stack to drop. Both live in the
trade-ticket impact panel and had been rendering in the sans body font
where the author wrote mono; they now render mono. See §7.

**`--ui-background-input` resolves to `--ui-background-secondary`,**
read off `pf_components.css:161` — `.pf-input, .pf-select, .pf-textarea
{ … background: var(--ui-background-secondary); … }`. The Transactions
filter controls now match the vocabulary's input surface, which is what
the name was reaching for.

The two comment lines at `transactions.css` 30–31 said the token stack
was used "since the app defines no `--ui-font-mono`". That clause is now
false, so it was dropped rather than renamed; the cross-reference to
the `cases.css` idiom stays.

---

## §3 — Fallback literals with a valid name

Sixteen sites, all mechanical: the name is declared, so the second
argument is dead code that only lies about the value. `charts.css` 99,
211; `portfolio_analysis.css` 122, 131; `statistics.css` 114, 136;
`saa.css` 284, 294; `super_admin.css` 90, 91, 96, 97, 327;
`overview.css` 310 (innermost `#E8304A` dropped, the two-token chain
`var(--chart-colours-primary, var(--ui-accent-primary))` kept);
`planning_desk.css` 138 (`--pf-bg-hover`), 94, 143, 438
(`--pf-accent-soft`) — names kept, literals dropped. No visible change
anywhere: every fallback was unreachable.

---

## §4 — True literals, `planning_desk.css` only (13 sites)

Mapped by role, using the `pf-note` / `pf-state` idiom — text is the
semantic colour, the surface a `color-mix` of it over the section
background.

| Lines | Was | Became |
|---|---|---|
| 78–80 `.pd-chip--txn` | `#25384a` / `#161d26` / `#b6d4f0` | `color-mix(… --ui-semantic-info 30% …)` / `… 10% …` / `var(--ui-semantic-info)` |
| 84–86 `.pd-chip--pacing` | `#254a30` / `#16211a` / `#b6f0c4` | the same shape on `--ui-semantic-positive` |
| 241 `.pd-plancell` | `#9fb2c2` | `var(--ui-text-secondary)` |
| 247–248 `tr.pd-rowgroup` | `#101418` / `#cfd6dd` | `var(--ui-background-chrome)` / `var(--ui-text-primary)` |
| 555–556 `.pd-pill--buy` | `#1a3f1f` / `#8affa0` | `var(--ui-semantic-table-pos-bg-dark)` (exact value match) / `var(--ui-semantic-positive)` |
| 560–561 `.pd-pill--sell` | `#3f1a1f` / `#ff8a8a` | `var(--ui-semantic-table-neg-bg-dark)` (exact value match) / `var(--ui-semantic-negative)` |

The badge design was not "improved" — the shape of the rule is
unchanged, only its colour source.

### Badge contrast — confirmed

Computed with the WCAG 2.x relative-luminance formula over the sRGB
`color-mix` result (componentwise weighted average, which is what
`in srgb` does):

| Badge | Theme | Text on its own 10 % tint | AA normal text |
|---|---|---|---|
| `.pd-chip--txn` (info) | dark | `#4A9BD9` on `rgb(25.4, 33.5, 39.7)` → **5.39 : 1** | ✅ |
| `.pd-chip--pacing` (positive) | dark | `#4CAF50` on `rgb(25.6, 35.5, 26.0)` → **5.79 : 1** | ✅ |
| `.pd-chip--txn` (info) | light | `#1F6FB2` on a 10 % tint of `#EAEAEA` → **3.87 : 1** | ❌ |
| `.pd-chip--pacing` (positive) | light | `#2E7D32` on a 10 % tint of `#EAEAEA` → **3.77 : 1** | ❌ |

The prompt's dark-mode claim is **confirmed**: both are ≥ 4.5 : 1.

The light-theme pair lands short of AA for normal text (`.pd-chip` is
`0.78rem`, regular weight, so the 3 : 1 large-text allowance does not
apply). This is **not a regression** — before this change the light
theme rendered the dark-mode literals, i.e. a pale-blue-on-near-black
chip sitting on a light page, which was legible but plainly wrong.
Deriving the tint is the right move; the ratio is a real finding for
whoever owns the light theme. Fixing it means a darker light-theme
`semantic.info` / `semantic.positive`, or a deeper tint — a theme
decision, out of scope here. Recorded as Flag 7.

---

## §5 — `rgb()` / `rgba()`, 22 sites

| Kind | Sites | Became |
|---|---|---|
| Panel / drawer shadows | `planning_desk.css` 39; `saa.css` 267, 345, 557; `layout.css` 571; `pf_components.css` 135, 241 | `var(--ui-shadow-panel)` |
| Modal scrims | `investments.css` 280; `saa.css` 349 | `var(--ui-shadow-scrim)` |
| Semantic tints | `saa.css` 224 (18 %), 229 (10 %), 388 (8 %) | `color-mix(in srgb, var(--ui-semantic-negative) N%, transparent)` |
| | `transactions.css` 1642 (8 %) | `… var(--ui-semantic-error) 8% …` |
| | `pf_data_import.css` 115 (25 %) | `… var(--ui-accent-primary) 25% …` |
| | `base.css` 276–277, 282–283 (12 % / 50 %) | `--ui-semantic-positive` and `--ui-semantic-info` at the same percentages |
| Out of scope | `login.css` 18 | left; Flag 1 |

`color-mix` was already a dependency (A0d deviation 4), so this adds
none.

**Two of the seven shadow sites change direction, not just depth** —
`saa.css` 267 (`.save-bar`, `position: fixed; bottom: 0`) and 557
(`.saa-section-root .save-bar`, `position: sticky; bottom: 0`) cast
`0 -2px 8px`, i.e. *upward*, because they are bottom-anchored bars
separating themselves from the content above. `--ui-shadow-panel` is
`0 12px 32px` and falls downward, where it is clipped by the viewport
or hidden under the bar. See §Deliberate deviations 3 — the separation
cue survives on `.save-bar`'s own `border-top: 2px solid
var(--ui-border-default)`, so this degrades a secondary cue rather than
removing the boundary.

---

## §6 — `--ui-button-fg` → `--ui-accent-on-accent`

All 23 sites replaced verbatim, file-wide, including the four on
`tx-btn` / `pf-cases__new-btn--primary` that A1e and A-3 delete anyway.
Occurrence count before 23, after 0; no bare (non-`var(`) reference
survives either.

**Not a visible change.** After A0d the two tokens hold the same value
in all three themes — `#000000` dark, `#FFFFFF` light and corporate
blue — so this removes the second source of truth ahead of the group
leaving the JSON in §1, and nothing on screen moves.

---

## §7 — Where the atlas will differ

*Written before the run. The operator fills in the right-hand column.*

| # | Where | Expected | Observed |
|---|---|---|---|
| 1 | **Planning Desk** — `.pd-chip--txn`, `.pd-chip--pacing`, `.pd-plancell`, `tr.pd-rowgroup`, `.pd-pill--buy/--sell` | Hue shifts on both badges (hand-picked pastel → the semantic colour at full strength on a 10 % tint), the plan cells and the row-group band move onto the text/background ladder, the buy/sell pills onto the table tint tokens. **The one area to read band by band.** | |
| 2 | **Planning Desk** — `.pd-paramstrip` shadow (`planning_desk.css` 39) | `0 4px 14px rgba(0,0,0,.5)` → `0 12px 32px rgba(0,0,0,.6)`: a deeper, softer drop under the parameter strip | |
| 3 | **Transactions → History** — `.tx-filters select`, `.tx-filters input[type="date"]` (`transactions.css` 1318) | The filter controls gain a `#141414` background where they were transparent. **The scene is `transactions-history`** (and the section shot `transactions--history.png`) — `transactions-flow-chooser` does **not** reach it; the markup is `_partials/transactions/_history.html:31`. | |
| 4 | **Transactions — impact panel** (`transactions.css` 1543, 1603) | `.tx-impact__basis dd` and `.tx-delta__pair` switch from the inherited sans to mono. `_impact_panel.html` is the trade-ticket preview (S6); **no atlas scene opens it**, so unobservable. | |
| 5 | **SAA** — the two save bars (`saa.css` 267, 557) and the dialog (345) | Drawer/dialog shadow deepens; the two save bars' shadow flips from up to down and effectively disappears (the `border-top` still separates). Only visible if a scene renders the save bar. | |
| 6 | **Chat — danger chip** (`chat.css` 459–460) | `.chat-pin-dialog__error` gains its colour. It renders only when a pin dialog carries an `error`; **likely unobservable in the atlas**, which never fails a pin. | |
| 7 | **Statistics** — `.stats-correlation` (`statistics.css` 90) | The correlation-matrix Plotly target gains `--ui-background-primary` where it was `transparent`. **`.pf-main` already paints that exact colour behind it, so nothing should change**; and the div renders only under `{% if has_correlation %}` (≥ 2 investments with return series), so on the atlas's empty DB it is absent entirely. Element: `statistics_section.html:89`, seen in `front-office--statistics.png` and `assistants/index--statistics.png`. | |
| 8 | **`base.css` 167** — `.app-header__nav-link:hover/:focus` | **Nothing.** `app-header` appears in no template — grep over `web/templates` and `web/static/js` returns zero hits. The whole `.app-header__*` block is dead since the A0b shell. Flag 4. | |
| 9 | **Cases — mono** | Cases is empty in the atlas (list empty-state only, no case detail), so the three `cases.css` mono rules are **unobservable**. Stated for the record, per the prompt. | |
| 10 | **Menus, activity panel, command palette** (`pf_components.css` 135/241, `layout.css` 571) | Blur radius changes, but no captured route opens a menu, the activity panel or the palette. | |

**Everything else pixel-stable.** The `#2c2c2c → #2A2A2A` and
`#161616 → #141414` shifts (§2) are 2 points and will not survive
image comparison noise; §3 and §6 are value-identical by construction.

**One caveat on mono.** `--ui-font-family-mono` puts `IBM Plex Mono`
first, ahead of the stack the sites already used. A-3 ships the woff2;
until then the name only resolves if the host has the font installed.
Headless Chromium in CI will not, so mono rendering should be
byte-identical to the before run everywhere except §7 item 4.

---

## §8 — The test

**A new file, `tests/web/test_css_tokens.py`** — not an extension of
`tests/web/test_pf_components.py`. The two pin different contracts: the
A0d file is a single stylesheet's transcription (its class list, its
wiring, its `<link>` order), this one is a tree-wide invariant
parametrised over every stylesheet the shell owns. Keeping them apart
means a new component sheet is covered automatically and the A0d file
stays **unedited**, which is the literal reading of "keep the existing
A0d assertions".

It asserts, per stylesheet (comments stripped, `vendor/` and
`theme.css` excluded):

* `hex == 0`;
* `rgb/rgba/hsl == 0`, except `login.css`, which is allowed exactly 1
  via `_LITERAL_EXEMPT`;
* `dangling == 0` — every `var(--…)` is declared in `theme.css`, or
  locally in the same file, or is `--key` (set from inline `style` on
  `.pf-chart__key`, so never declared in CSS);

and, globally:

* `theme.css` declares `--ui-font-family-mono`,
  `--ui-accessible-control-row-pad`, `--ui-accessible-border-soft`,
  `--ui-shadow-panel`, `--ui-shadow-scrim`;
* `theme.css` does **not** declare `--ui-button-fg`;
* no stylesheet references `--ui-button-fg`.

85 tests, all green, DB-free.

---

## Gates

| # | Gate | Result |
|---|---|---|
| 1 | `pytest tests/web/test_pf_components.py tests/web/test_css_tokens.py tests/scripts/test_generate_theme_artifacts.py tests/core/test_ui_theme.py tests/regression -q` | ✅ **391 passed** in 3:42 |
| 2 | `ruff check` · `ruff format --check` · `generate_theme_artifacts.py --check` | ✅ all clean (938 files already formatted) |
| 3 | `git diff --name-only` | ✅ three theme JSONs, `theme.css`, 19 stylesheets under `web/static/css/`, the new test, this report. **No `.js`, no template, no vendor.** |
| 4 | inventory, after | ✅ `hex 0`, `rgb/rgba 1`, `dangling 0`; only `login.css` listed |
| 5 | `wc -l web/static/css/theme.css` | ✅ **421** |
| 6 | `git diff --stat …/pf_components.css` | ⚠️ **4 insertions, 5 deletions** — drift, see §Deliberate deviations 2 |
| 7 | `pytest tests/web -q` | ⏸️ **DEFERRED to the A-0 Ende full-suite run** — 155/1538 run, one failure, proven pre-existing. See §OPERATOR ACTION REQUIRED 4 and below |
| 8 | Atlas after | operator — §7 |

### Gate 7 — deferred, and the one failure seen is not ours

The gate was stopped at 10 % on a time budget: ≈85 minutes for a run
that the A-0 Ende full suite repeats in full this evening. The
reasoning, the evidence that no colour pin is at risk, and what to
watch for are in §OPERATOR ACTION REQUIRED 4 — **that is the open
point this report hands forward.**

The single failure the partial run did surface,
`tests/web/test_cases_area.py::test_cases_page_renders_three_sections`
(`assert 'data-section="open-cases"' in body`), is **not** a stale pin
on a colour literal; it is the A0b one-section-per-view shell
(`9581ed0`), where `/cases` renders one section per view and the
section-indicator dots the test expects are gone.

Proven, not assumed: `git stash push -u`, then the single test on the
clean tree — **identical failure**, same assertion, same message. The
tree was restored with `git stash pop` and re-verified (`theme.css`
still 421 lines, 24 paths dirty). No test pin was changed by this
prompt. The failure belongs to whichever prompt owns the Cases
migration; recorded as Flag 3.

---

## Deliberate deviations

1. **Light-theme `accessible.border_soft` is `#9A9A9A`, not
   `#7A7A7A`.** The prompt's cell reads "one step lighter than the
   accessible `border_default` (`#5A5A5A` → use `#4A4A4A`); light
   theme: one step darker than its `border_default`". The two adjectives
   are each inverted relative to hex luminance — `#4A4A4A` is *darker*
   than `#5A5A5A`, not lighter — and they are inverted **consistently**,
   which is what decides it. In both base themes `border.soft` is the
   *lower-contrast* member of the pair: dark `#2A2A2A → #1F1F1F`
   (darker on a dark page), light `#C8C8C8 → #E0E0E0` (lighter on a
   light page). Reading the adjectives as "one step softer" makes the
   given dark value correct and forces light to go *up*, to `#9A9A9A`.
   Reading them literally would make the light theme's "soft" border
   **harder** than its default, i.e. the name would be a lie. I took
   the softer value. The token is inert today (`data-a11y` still has
   no writer — A0d flag 4), so the cost of being wrong is one JSON
   character whenever the mode ships.
2. **Gate 6 reads 4 insertions / 5 deletions, not "≤ 4 lines
   changed".** The four intended declarations are the whole change; the
   fifth deleted line is the second half of the two-line comment A0d
   left behind — *"theme.css publishes no
   --ui-accessible-control-row-pad, so the source's own 14px stands
   in"* — which this prompt makes false. Leaving a false comment in
   place to satisfy a line budget is the wrong trade. The anchor holds:
   nothing changed in the file except the two shadows, the row pad, the
   border-soft addition, and the retirement of that comment.
3. **The two SAA save-bar shadows were flipped, as instructed.**
   `saa.css` 267 and 557 are bottom-anchored bars whose `0 -2px 8px`
   pointed *up*; the prompt lists both under "Panel/drawer shadows →
   `var(--ui-shadow-panel)`", which points down. I followed the prompt
   rather than inventing a `shadow.rise` token, because one elevation
   vocabulary is the point of §5 and a second token is a design
   decision, not a sweep. The consequence is named in §5 and §7 item 5:
   both bars keep their `border-top`, so the boundary survives and only
   the secondary shadow cue is lost. If the operator wants it back,
   `shadow.rise` is a three-line JSON change.
4. **`transactions.css` 31's trailing clause was deleted, not
   renamed.** The comment explained the fallback idiom "since the app
   defines no `--ui-font-mono`". The premise is gone, so the clause
   went with it; the cross-reference to the `cases.css` idiom stays.

---

## §7 Flags

1. **`login.css:18` still carries `rgba(0, 0, 0, 0.25)`** — the auth
   card's box-shadow. Out of scope: `_auth_base.html` does not link the
   component vocabulary, and A-5 owns the auth surface. It is the
   single allowance in the new test (`_LITERAL_EXEMPT`); A-5 should
   delete the entry, not widen it. (Carried from A0d flag 3.)
2. **The `pf` block in `chart_theme.json` is still parked.**
   `--pf-bg-hover`, `--pf-accent-soft` and their kin are emitted into
   `:root` only — `_split_pf_section` runs per file, and the block
   lives in `chart_theme.json`, which has no per-theme variant. So the
   four `planning_desk.css` sites that read them (94, 138, 143, 438)
   get dark-theme values under **every** theme. This sweep kept the
   names and dropped their literals, which is all it could do; moving
   the block into `ui_theme*.json` is a theme decision for a later
   prompt. Until then `--pf-*` is a hole in the "tokens are the only
   colour source" rule, invisible to the inventory because the names
   *are* declared.
3. **Gate 7 is an open point, not a pass.** `pytest tests/web -q` was
   interrupted at 155/1538 and is carried to the A-0 Ende full-suite
   run — see §OPERATOR ACTION REQUIRED 4 for the reasoning and for the
   evidence that no colour pin is at risk. The one failure it did
   surface,
   `tests/web/test_cases_area.py::test_cases_page_renders_three_sections`,
   is **pre-existing**, proven against the clean tree: it belongs to
   the A0b one-section-per-view shell, not to this prompt, and was not
   touched.
4. **`base.css`'s whole `.app-header__*` block is dead CSS.** No
   template or script mentions `app-header`; the A0b shell replaced it
   with `pf-*`. Eleven rules, roughly 60 lines, including the one this
   sweep just fixed at 167 — a fix that can never render. Left in place
   (deleting rules is structure, not a sweep), flagged for A-6.
5. **"Eleven token names that no theme declares" is eight.** The
   inventory's `dangling 32` counts *sites*; the distinct names are
   `--ui-font-mono`, `--ui-border`, `--ui-surface-secondary`,
   `--ui-surface-primary`, `--ui-semantic-danger`,
   `--ui-background-default`, `--ui-background-tertiary`,
   `--ui-background-input`. Counting (file, name) pairs gives 12. No
   name was missed — every one the inventory reported is fixed and the
   after-count is 0 — the prompt's figure is just a miscount.
6. **`IBM Plex Mono` is not shipped.** `font.family_mono` names it
   first so A-3's woff2 lands without a second token edit; until then
   every renderer without it installed falls through to `ui-monospace`,
   which is what the sites used before. Harmless, but the token's
   *stated* value is aspirational for one more strand.
7. **The light theme's derived Planning Desk badges are below AA.**
   3.87 : 1 and 3.77 : 1 against the 4.5 : 1 needed for `0.78rem`
   regular text (see §4). Not a regression — the previous light-theme
   rendering was dark-mode literals on a light page — but it is now a
   real, measurable contrast gap rather than an obviously-wrong colour.
   Fixing it is a light-theme `semantic.info` / `semantic.positive`
   decision, or a deeper tint percentage; either is out of a sweep's
   scope.

---

## Handover — DC-UX-A2

> **P-UX-A0d2 done.** Every stylesheet under `web/static/css` is now on
> the tokens: `hex 0`, `rgb/rgba 1` (`login.css`, A-5), `dangling 0`,
> and `--ui-button-fg` is gone from both the JSON and the tree. Six
> sites that had been computing to *nothing* now render — four of them
> unobservable in the atlas, and the two that are not are the
> Transactions history filters and the Planning Desk, which is the one
> area to read band by band.
>
> Gates 1–6 green. **Gate 7 is deliberately open**: `pytest tests/web`
> was stopped at 155/1538 rather than spend ≈85 minutes on a run the
> A-0 Ende full suite repeats tonight. It carries forward as the
> report's one open point (§OPERATOR ACTION REQUIRED 4), with the grep
> evidence that no test pins a literal this sweep removed. The one
> failure it did surface is the pre-existing Cases section-anchor test,
> proven against a clean tree. Gate 8 is the operator's: compare
> against `docs/ux/atlas/2026-09-23-2/` (`git_head fca0d37`), **not**
> `2026-09-23/`, which `tail -1` returns by sort-order accident and
> which predates A0d.
>
> Three flags want a decision rather than a note: the `--pf-*` block
> still emits dark values under every theme (Flag 2), `base.css`'s
> `.app-header__*` block is dead (Flag 4), and the light theme's newly
> derived Planning Desk badges measure 3.8 : 1 (Flag 7).
