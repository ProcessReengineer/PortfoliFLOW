<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (c) 2025-2026 Sönke Pinkernelle -->

# PortfoliFLOW UI standards

The rules every surface is built against, and where each one lives in the
code. Source: the DC-UX-D design-parameters record (v4), restated against the
product. A rule that is not yet implemented says so and names the strand that
owns it. Values are never restated — they are generated from
`config/ui_theme*.json` and this document links to them.

---

## 1. Where things are

| Layer | File | What it holds |
|---|---|---|
| Token source | `config/ui_theme.json`, `…_light.json`, `…_corporate_blue.json` | Every colour, size, space and duration. **New tokens go here.** |
| Token generator | `scripts/generate_theme_artifacts.py` | JSON → CSS. Runs from the pre-commit hook. |
| Generated tokens | `web/static/css/theme.css` | 421 lines, 249 `--ui-*` declarations. **Never hand-edited.** |
| Shell layout | `web/static/css/layout.css` | The grid, sidebar, view head, status bar, Shirley's column and stage. |
| Component vocabulary | `web/static/css/components/pf_components.css` | The one set of classes every surface builds from (§4). |
| Legacy component sheets | `web/static/css/components/*.css` | Per-area sheets, migrating onto §4 strand by strand. |
| A finished migration's residue | `web/static/css/components/transactions.css` | What is left when an Area has moved: 150 lines, **no `tx-` class rule at all**. Nine groups, each scoped to `.pf-transactions`, each painting a *shared* family in a state or at a measurement the record does not name — the five mono-holdout ids; a `pf-field` holding a control; a missing fact on `pf-context` and its input form on `pf-field`; a choice that refuses; the gap between a note and a filter bar; `pf-leg`'s 124 px type track; and the negative-cash wrapper's margin with its `:empty` collapse. Every group names the record answer that retires it. |
| Projection family | `pf_components.css` — `pf-lenses`, `pf-lens__title`, `pf-delta` + `__label` / `__before` / `__arrow` / `__after` / `__flag` | Before → after across several lenses: **one grid per lens**, five cells a row so the columns line up down a lens, and the tone on the flag and nowhere else. Transcribed from the record for the Transactions impact panel; one user. |
| Chart theme | `config/chart_theme*.json` → `services/chart_specs/_theme.py` | Series colours, canvas, axes. **Not UI tokens** (§2.8). |
| Icons | `web/icons.py` (`ICONS`, `pf_icon`), `web/static/vendor/lucide/` | One vendored open-source set, inlined as SVG at 16 px / 1.5 px stroke. |
| Section catalogue | `web/shell.py` (`_AREAS`, `_SECTIONS_BY_AREA`, `sections_for`) | What exists per Area, its order, its landing view, its role gate. |
| Shell frame | `web/templates/base.html` | The grid, the skip link, `{% block shirley %}`, the script order. |
| View frame | `web/templates/areas/_section.html` | The sticky view head and the section body. One include per view. The as-of span is rendered **unconditionally**, with `id="{section_slug}-asof"`, so a body can fill it out of band. |
| Sub-surface head | `web/templates/_partials/transactions/_composer_head.html` | The crumb and the ident/state pill a sub-surface writes into the section head out of band (`#{slug}-title`, `#{slug}-asof`). **One head for five surfaces** — the four composers and the wizard. |
| Navigation | `web/templates/_partials/sidebar.html`, `_partials/command_palette.html` | Both levels of the nav, and the Ctrl K surface. |
| Status bar | `web/templates/_partials/statusbar.html` | Area, tenant, source link, build id, config state. |
| One section per view | `web/static/js/shell.js` | The only thing that moves the `hidden` attribute. A sub-surface that swaps into a hidden section hands it the **fragment** rather than touching `hidden`: the composer host carries `hx-on::after-swap` setting `location.hash = 'new'`, guarded both on the swap's own target (`afterSwap` bubbles, and every recalculation swaps inside the host) and on the hash it already holds (assigning the current hash fires no `hashchange`). |
| The keyboard | `web/static/js/section_nav.js` | Ctrl K and Ctrl J, one file for the shell's shortcuts. |
| Shirley's states | `web/static/js/shirley.js` | The only thing that writes `data-shirley`. |
| Number entry | `core/decimal_input.py`, `web/templates/_partials/transactions/_readings.html` | R10's server-side parser — both notations, `Decimal` and never `float`, `None` for anything it cannot read — and the `pf-read` "Read as …" echo it feeds out of band beside each field. |

---

## 2. Rules

Record §2, one rule per line, with the product's implementation beside it.
"Not yet — strand X" means the rule stands and the code does not have it.

### 2.1 Navigation model

| # | Rule | Implementation |
|---|---|---|
| 1 | Persistent sidebar is the primary navigation; one icon per Area. | `sidebar.html`; `ICONS` maps the nine Area slugs to Lucide stems. |
| 2 | The active Area lists its sections as a second level; the right-edge indicator is dropped. | `sidebar.html:55–61`, `data-pf-section-link`; the indicator is gone. |
| 3 | One section per view; the fragment selects it; hidden sections never load. | `shell.js`; `areas/_section.html` renders every non-landing section `hidden`; 31 bodies load on `intersect once`, and a `display:none` element never intersects. |
| 4 | **R11** An Area may carry a context strip above the view; Scenario Analysis loads on `load`, the one exception. | `#pd-paramstrip` in `_planning_desk_body.html`, filled out of band. |
| 5 | A section that stacks several questions shows them as in-view tabs. | `.pf-tabs` / `.pf-tab` exist in the vocabulary; no view uses them — strand A-2 (Back Office) and A-4 (Admin). |
| 6 | A sub-surface replaces the view and renders `‹ Parent › Sub`. | **Built in Transactions** — `_composer_head.html` writes the crumb into the section head out of band, and the way back restores the plain heading and an empty stamp; 2 templates. `‹` returns to the **parent surface**, not to the row the sub-surface was opened from — a record question (§2.1 has no rule for origin). Strands A-3 and A-4 still owe theirs. |
| 7 | Command search is a visible field in every view head; Ctrl K unchanged; shortcuts written "Ctrl K" and shown in the control's tooltip. | `areas/_section.html:49–50`, `aria-keyshortcuts="Control+K"` and a `<kbd>Ctrl K</kbd>`. The collapse to an icon below 994 px is not yet (§5, container queries). |
| 8 | Long section titles wrap; no ellipsis. | `.pf-sidebar__section` sets `min-height` and `line-height`, never `white-space: nowrap`. |
| 9 | The second level and the command search list only what the role can open. | `sections_for(area, is_tenant_owner=…)`; `owner_only` on the catalogue entry. |

### 2.2 Page anatomy

| # | Rule | Implementation |
|---|---|---|
| 1 | Shell grid: nav (252 / 56 px) · work area · Shirley (44 / 380 px). Below 1600 px the nav collapses while Shirley is open. | `.pf-shell` names three columns and two rows; the auto-collapse is **not yet** — strand A-5. |
| 2 | Sticky view head: title or crumb · as-of · command search · at most one primary action. | `.pf-view__head` (`position: sticky; top: 0`); slots `section_as_of` and `section_primary_template`. |
| 3 | Content column max 1664 px; administrative lists and forms at 960 px. | `--ui-layout-content-max` on `.pf-main`; `.pf-narrow` exists, no user yet — strand A-4. |
| 4 | Layout responds to the **content column**, never the viewport; minimum 1280 px. | **Not yet** — `.pf-main` declares no container (§5.4). The first *layout-level* consequence has arrived: the impact panel's three lens columns do not collapse inside a table slot. Measured at 1280 px the two-column form is comfortable (824 px main column, 264 px fields); the case that hurts, 968 px, is already below the minimum this rule answers for and is the nav auto-collapse's to fix, not a component's. |
| 5 | Chart triplet: 3 across, else 2+1, stacked below 684 px. | **Not yet** — strand A-5 (Front Office). |
| 6 | **R5** Detail opens in a slot row under the row; a sub-surface with a crumb for creation; `<dialog>` only off-view and for the command search. No side panel — the right edge is Shirley's. | The command palette is the one `<dialog>`; slot rows are the Transactions anatomy (`#tx-detail-{id}`). |
| 7 | Depth by surface — chrome / work area / panel / raised — with one border colour; no card inside a card. | Four `--ui-background-*` tokens, one `--ui-border-default`. |
| 8 | The status bar stays: area, tenant, activities, source link, build id, config state. | `statusbar.html`; the activities list is **not yet** — strand A-5. |

### 2.3 Action hierarchy

| # | Rule | Implementation |
|---|---|---|
| 1 | One button family, `pf-btn`: primary · default · quiet · danger (outlined) · icon · sm. It replaces `btn`, `tx-btn`, old `pf-btn`, `pf-dc-btn`. | Declared in full; **58 sites across 25 templates**. **`tx-btn` is gone** — A-1 took it from 40 to 0. `btn` (174 sites, 75 files) and `pf-dc-btn` (9 sites) are still live; each Area strand migrates its own. |
| 2 | **R1** At most one primary per view; the accent never appears inside a repeated item. | Rule stands; enforced per strand, not by code. Pinned in Transactions by six `…_carries_exactly_one_primary` tests over the four composers and the wizard's four steps. The rule reaches **inside a sub-surface**: a panel that opens in a table slot is part of its view, so it offers no primary of its own — the cancel and reverse panels lead with `pf-btn--danger`, and a list never carries the accent. |
| 3 | Peer answers form a decision set of default buttons; none is primary. | Watch Desk — strand A-6. |
| 4 | Form outcomes in a sticky action bar with a hint sentence; **R3** leading slot for Back. | **Built** — 8 templates. `pf-actionbar` is a **form outcome** bar and never sits inside a `pf-panel`: it is sticky over `--ui-background-primary`, so on a panel's ground it paints a mismatched strip that clings to the viewport while the table scrolls behind it. A panel closes with **`pf-panel__foot`** instead — danger first, then the quiet way out — and R3's leading slot is the action bar's rule, not the foot's, which has no hint to lead. |
| 5 | The row is the open gesture; at most one quiet secondary; destructive acts in a `<details>` menu. | **Built** — the Blotter and History rows, 2 templates. The gesture sits on the row's **id button**, not on the `<tr>`: a row handler would also swallow the menu's `<summary>` and the quiet secondary beside it. Whether a delegated handler should take the whole row instead is a record question. |
| 6 | **R7** Confirmation is inline, verbatim, with a danger button. No `confirm()`, `alert()` or `hx-confirm`. | **Built** — the cancel and reverse panels draw `pf-btn--danger` beside a quiet *Keep* in a `pf-panel__foot`; 2 templates. The way out is `this.closest('td').innerHTML = ''` — the panel asks the server nothing to go away, and emptying the cell is also what un-rotates the row's chevron (§2.4.2). |
| 7 | A control the route will refuse is disabled with the refusal as visible text. | `.pf-btn[disabled]`; strand A-4. |
| 8 | Shirley's Send takes the accent only while the box holds text. | Strand A-7 (Assistants). |
| 9 | **In the shell** red is the one primary action, the active nav marker, focus and the logo — nothing else. **In charts** the chart theme governs verbatim; red leads there. | `--ui-accent-primary` vs `--chart-colours-primary`; `--ui-text-primary` is neutral since A-0. |

### 2.4 Tables and rows

| # | Rule | Implementation |
|---|---|---|
| 1 | One table look, `pf-table`: low-contrast header, right-aligned figures, optional sub-line, hover by surface. | **Built** — the Blotter and History, 2 surfaces over 4 templates, with `__row` / `__sub` / `__unit` / `__slot` / `__actions` / `__id` / `pf-num`. |
| 2 | Detail opens in a slot row under the row; one open at a time per table. | `.pf-table__slot`. **`is-open` is written by CSS, not by script** — `.pf-table__row:has(+ .pf-table__slot > td:not(:empty), + .pf-table__slot + .pf-table__slot > td:not(:empty))` in the component layer, so filling or emptying the cell rotates the chevron with no handler anywhere. The class survives beside the selector for a later script writer. Whether `:has()` is the sanctioned writer is a record question. |
| 3 | **R6** Leading chevron = expands in place; trailing chevron = navigates. | `.pf-table__chev`. |
| 4 | Sort state shown in the header and exposed through `aria-sort`. | `.pf-table th[aria-sort]`. |
| 5 | Plain HTML with HTMX slots is the default engine; Tabulator may stay where client-side sort is needed, skinned the same. | `components/tables.css` skins Tabulator; both engines, one look. |
| 6 | Signed figures take the sign colour as text; no cell backgrounds; ADR-0062 §2 thresholds stay. | `.pf-table td.pf-pos` / `td.pf-neg`. `pf-neg` had **no user at all** until A-1's last prompt drew it in five templates — four in Transactions plus the investment detail — so it is now inline prose as well as a cell, and ADR-0062 §2's wording question (Q-UX-D-14) is cross-Area and open. `pf-pos` still has none. |
| 7 | **R6** Grouped table: a disclosing group header row; no interactive content in `<summary>`. `pf-meter` for utilisation cells. | `pf-meter` is **not built** — strand A-6. |
| 8 | Several small forms in one slot share one panel as action groups. | `.pf-acts` / `.pf-act`. |
| 9 | No selection model in this iteration. | Nothing acts on several rows at once. |

### 2.5 Forms

| # | Rule | Implementation |
|---|---|---|
| 1 | Labels above, hints below, "(optional)" verbatim, numbers right-aligned. | `.pf-label` / `.pf-hint` / `.pf-optional` / `.pf-input--num`. |
| 2 | Small exclusive choices are a segmented control; optional groups collapse. | `pf-seg` is **built** — the order composer's Direction, two radios kept as radios; the checked state is carried by ground **and** weight, so §2.11.1 holds with no rule of the Area's. One user. The "optional groups collapse" half is not done anywhere: no group in the book is a `<details>` yet. |
| 3 | Derived figures sit in a sticky summary rail placed by CSS grid; DOM order and `hx-include` unchanged. | **Built** — 4 rails. The rail **is** the recalculation's swap target (`#tx-derived`) and the form's second child, so `.pf-form`'s own `minmax(0, 1fr) 360px` places it with no rule of the Area's — no host wrapper, no `display: contents`. Two consequences hold generally: the settlement block sits **inside** the rail, so one swap covers every block in it; and a facts strip stays in the **main column**, because an element carrying `hx-swap-oob` inside the swap target is lifted out of the fragment before the fragment lands, and the rail would arrive without it. |
| 4 | **R4** Stepped surfaces: a non-interactive `pf-stepper`; one stable form column. | **Built** — the wizard runs on it: `<ol class="pf-stepper">`, one `aria-current="step"`, and no `hx-`, no `<button>` and no `<a>` between the list's tags. **Authored from the record's specification, not transcribed** — `shared.css` has no stepper family, and its `.pf-step` belongs to the setup checklist (§2.9.2), so the item is `pf-stepper__step`: two families that share a block name share a fate. The column is stable because `.pf-form` declares both tracks on **every** step and only the figure step fills the second. One user. |
| 5 | Validation: the refusal verbatim, first, inside the surface that caused it; `aria-invalid` and a border, never colour alone. | `.pf-input[aria-invalid="true"]`. |
| 6 | **R10** Number fields accept both notations, parsed **server-side**; fields become `type="text" inputmode="decimal"`. Dates stay native. | **Done in Transactions.** `core/decimal_input.py` reads both notations into a `Decimal` (never a `float`) and returns `None` for anything it cannot read — the endpoint still refuses nothing. 16 amount fields are `type="text" inputmode="decimal"`; the two vintage **years** are `inputmode="numeric"` with no echo, because a year is not an amount. The recalculation echoes *Read as …* into a `pf-read` slot beside each amount field, and that echo is load-bearing: a **lone** separator reads as the *decimal* separator, so `1,234` is one-point-two-three-four and the operator has to be told. **20 `type="number"` remain outside the Area** — investments 7, planning desk 4, SAA 4, watch desk 3, portfolio analysis 2. Each sweep inherits the parser and owes the **echo** as well: a field that accepts two notations and says nothing is worse than one that accepts one. |
| 7 | Confirmation and outcome screens keep their content; legs and effects use `pf-leg` / `pf-facts`. | Both **built** — `pf-leg` in 6 templates, `pf-facts` in 2. The family the record calls `pf-kv` is declared here as **`pf-facts`**; `pf-context` is its sibling and differs only in which way the margin runs (`0 0 space-5` against `space-4 0 0`), so a strip *inside* a panel takes `pf-facts` and one *under* a field grid takes `pf-context`. `pf-leg` was drawn for `buy` and `sell`: its 34 px type track does not hold a longer effect vocabulary, and the type carries one tertiary ink whatever the leg does — both are record questions, and the Area's stand-in is a scoped 124 px track (§4). |

### 2.6 Status and feedback

| # | Rule | Implementation |
|---|---|---|
| 1 | Functional state is a dot plus a word, never a filled pill; never colour alone. | `.pf-state` with seven state modifiers plus `--dormant`; 5 templates. `--dormant` still has no user: the `auditor` role is its declared one, and an *approved* ticket is not dormant — the row acts on it. |
| 2 | One note pattern, `pf-note`, in three tones, with an optional inline action and a `pf-more` disclosure. | `.pf-note--info` / `--warn` / `--block`; **17 templates**, the most-used family after `pf-btn`. The action slot is the third `auto` grid column — a **position, not a class**: a note with no action fills it with an empty `<span>`. The record should either bless the `pf-btn--quiet pf-btn--sm` that now sits there or grow a `pf-note__action`. There is no `--done` tone; `--info` plus `pf_icon("done")` stands in, which leaves `--ui-semantic-success` with no user. |
| 3 | Service refusals are shown verbatim and first. | Rule stands; per-surface. |
| 4 | Explanatory prose moves behind `pf-more`; internal identifiers leave visible copy. | `.pf-more`; 6 templates. The identifier half is measurable: moving the five flow codes out of the chooser's visible copy into `data-flow` took the Transactions **abbrev** finding from 13 to 3. |
| 5 | Long-running jobs live in the status bar and survive navigation. | `.pf-activity` declared; the list is **not yet** — strand A-5. |
| 6 | **R9** Every figure-bearing view carries an as-of stamp in its head. | `areas/_section.html` renders the span **unconditionally**, with `id="{slug}-asof"`, so a lazy body fills it out of band; the Blotter and History do. On a **live list the stamp is render time, in UTC** — it is not a property of the data. The Overview states its `HH:MM` in the market-data schedule's timezone instead, so the book now carries **two clocks**; the record has not chosen between them. A sub-surface reuses the same slot for its ident and state pill, which is why leaving one restores it empty. `.pf-asof` (the richer pair form) still has no user. |
| 7 | **R12** One disclosure pattern for provenance — Shirley's sources line and the Watch Desk's derivation. | `.pf-sources` renders in the chat; the Watch Desk half is strand A-6. |
| 8 | **R8** A finding is a toned panel: band as dot plus word, trigger as title, no box inside it. | `pf-finding` is **not built** — strand A-6. |
| 9 | JSON-backed surfaces render notes and inline confirmations through one shared client helper. | **Not yet** — strand A-4 (`/investments`). |

### 2.7 Progressive disclosure by role

| # | Rule | Implementation |
|---|---|---|
| 1 | Disclosure is designed on the one enforced axis, owner vs member. | `is_tenant_owner` throughout the shell. |
| 2 | What a role cannot open is not offered — not in the nav, not in the search, not as a control. | `sections_for()` filters both; the route gate stays the gate. |
| 3 | Owner-only today: Admin › Users, Admin › Market Data, the tenant panel of Providers & Credentials, all `/investments` writes. | `owner_only=True` on two catalogue entries; the rest are route gates. |
| 4 | Personal settings live under "My settings", open to every role. | **Not yet** — strand A-4. |
| 5 | The `auditor` role is dormant; no read-only surface is designed here. | `.pf-state--dormant` is the one addition to the transcribed vocabulary. |

### 2.8 Tokens

One family (IBM Plex Sans, self-hosted `woff2`, 400/500/600), five sizes, a
4 px spacing scale, two radii, one border colour, one motion duration for
state changes only. One icon set, inlined as SVG at 16 px / 1.5 px stroke; an
icon stands alone only with an `aria-label` and a tooltip. Every asset is
vendored locally (ADR-0037 §9). **Charts are not UI tokens** — see §3.

### 2.9 Empty, loading and error states

| # | Rule | Implementation |
|---|---|---|
| 1 | **R2** Empty: one sentence plus the view's primary action. A calm state is an empty state without an action. | `.pf-empty` / `.pf-empty__lead`, 3 templates; the legacy `pf-empty-state` is still in 12. A list's empty state carries **no** action even where the record draws one — R1 keeps the accent off a list, and the view's primary lives where the thing is created. |
| 2 | A setup checklist stands where the empty states stand while the book is empty, and disappears by itself. | `.pf-setup` / `.pf-step` declared; **not wired** — strand A-5. |
| 3 | Loading: the lazy partial renders the skeleton of its own section, with `aria-busy`. | `.pf-skel` declared; the lazy shells still render "Loading…" text — per-strand. |
| 4 | Error: a block-tone note in place, verbatim, with the way out as its inline action. | `.pf-note--block`. |

### 2.10 Assistants surfaces

| # | Rule | Implementation |
|---|---|---|
| 1 | Shirley is a shell element outside `#shell-main`, three states on `.pf-shell[data-shirley]`: closed, docked (pushes, never overlays), stage. Ctrl J toggles. | `base.html` `{% block shirley %}`; `shirley.js`; the grid's third column is the push. |
| 2 | One DOM instance, moved between dock and stage, never duplicated. | `shirley.js` `relocate()`; `#chat-history` and `#chat-form` stay unique, so a running stream survives navigation. |
| 3 | Answers carry the sources line; the model line moves to Providers & Credentials. | `.pf-sources` renders; the model line has **no home** — strand A-7. |
| 4 | Shirley does not receive the current view as context; the "Consulting for" banner is the only context mechanism. | The case brief stash; no view context anywhere. |
| 5 | The Watch Desk is not conversational — it takes the tabular anatomy. | Strand A-6. |

### 2.11 Accessibility

| # | Rule | Implementation |
|---|---|---|
| 1 | WCAG AA contrast, a visible focus ring, keyboard reachability, state never by colour alone, `prefers-contrast` and `prefers-reduced-motion` honoured. | `--ui-focus-*`; `@media (prefers-reduced-motion: reduce)` and `(prefers-contrast: more)` in `pf_components.css`. |
| 2 | Personal "Accessible mode", one attribute `data-a11y="on"`: ~120 % type, stronger contrast, 44 px targets, underlined links and quiet buttons, no motion. | The remap block is complete and correct. **Nothing writes the attribute** — strand A-4 ("My settings"). Set it by hand in the console to test. |
| 3 | Every view must hold at 125 % text: nothing clips, tabs and tool rows wrap, wide tables scroll inside their own container. | `.pf-tabs` wraps; `.pf-scroll-x` exists, no user yet. The checklist in §8 is how this is verified. |

### 2.12 Chart identity

| # | Rule | Implementation |
|---|---|---|
| 1 | The chart language is kept: red-led series, dark canvas, no gridlines, thin grey axes. The shell's reduction does not touch it. | `config/chart_theme.json`, unchanged by this track. |
| 2 | A chart is the theme's plot inside a `pf-chart` frame; the canvas is `colours.background`; no second border between frame and plot. | `.pf-chart` reads `--chart-colours-plot-area`. |
| 3 | Chart titles and series names are the specs' own values. | `services/chart_specs/`. |
| 4 | Tile-sets follow ADR-0082 per archetype. | Front Office — strand A-5. |
| 5 | KPI captions above a tile-set are a label/value line, not pills. | `.pf-figures`; strand A-5. |
| 6 | Chart text follows the product typeface. | `--chart-font-family` leads with IBM Plex Sans; `.pf-chart__plot text` uses `--ui-font-family`. |

---

## 3. Tokens

Source of truth is `config/ui_theme.json` and its two siblings; the generated
result is [`theme.css`](../../web/static/css/theme.css). Adding a token means
editing the JSON and re-running `scripts/generate_theme_artifacts.py`.

| `--ui-*` group | JSON key path | Intent |
|---|---|---|
| `--ui-background-*` | `background.{chrome,primary,secondary,section}` | The four depths: chrome, work area, panel, raised. Depth is carried by surface, not by shadow. |
| `--ui-accent-*` | `accent.{primary,primary_hover,primary_pressed,on_accent,logo}` | The one primary action, the active nav marker, focus, the logo. Black on the accent is 4.95:1. |
| `--ui-text-*` | `text.{primary,secondary,tertiary,disabled,on_sidebar}` | Neutral type since A-0 — `primary` is no longer the brand red. |
| `--ui-border-*` | `border.{default,soft}` | One border colour plus a row separator. |
| `--ui-font-family`, `--ui-font-family-mono` | `font.family`, `font.family_mono` | One family, self-hosted; the mono stack for identifiers and figures. |
| `--ui-font-scale-*` | `font.scale.{xs,sm,md,lg,xl,hero}` | Five sizes plus the one hero figure. |
| `--ui-font-weight-*` | `font.weight.{regular,medium,semibold}` | Three weights; no fourth is vendored. |
| `--ui-semantic-*` | `semantic.*` | Meaning colours: positive, negative, warn, info, plus the ADR-0062 table tints. |
| `--ui-space-*` | `space.1…7` | The 4 px scale: 4 / 8 / 12 / 16 / 24 / 32 / 48. |
| `--ui-radius-*` | `radius.{sm,md}` | Controls; panels and charts. |
| `--ui-control-*` | `control.{height,height_sm,input_height,row_pad}` | Tokenised so accessible mode can raise them. |
| `--ui-motion-duration` | `motion.duration` | State changes only — dock, nav, chevron. No decorative motion. |
| `--ui-focus-*` | `focus.{width,offset}` | The focus ring, on every focusable element. |
| `--ui-shadow-*` | `shadow.{panel,scrim}` | Whole CSS values, not colours: the menu/palette drop shadow and the modal scrim. The only elevation in the system. |
| `--ui-layout-*` | `layout.*` | Nav, rail, dock and stage widths; content max 1664 px; reading width 960 px. |
| `--ui-accessible-*` | `accessible.*` | The `data-a11y="on"` overrides. Remapped onto the base names by `pf_components.css`, not by the generator. |

### Chart tokens a component may read

Two only, and only to sit a frame flush against a plot:
`--chart-colours-plot-area` and `--chart-colours-text`. Everything else under
`--chart-*` belongs to the plot and is read by `services/chart_specs/`, never
by a stylesheet.

### `--pf-*` — parked cleanup

`theme.css` still emits a legacy shell block: sidebar widths, the status-bar
height, accent overlays, the sticky-header pair, the palette geometry, and
five `--pf-section-indicator-*` tokens whose feature A0b retired.
`--pf-statusbar-height` is load-bearing — both sticky columns compute against
it; the rest is the cleanup. **Do not add to this block.**

---

## 4. Component catalogue

`web/static/css/components/pf_components.css`, **59 `pf-` block names** —
50 transcribed whole from the record's `shared.css` onto `--ui-*` tokens in
A0d, and **nine added by strand A-1**, marked **A-1** below. Two rules govern
the file: no colour literal, no `--pf-*` reference.

```sh
# the block names
grep -oE '^\s*\.pf-[a-z0-9_-]+' web/static/css/components/pf_components.css \
  | sed -E 's/^\s*\.//; s/(__|--).*//' | sort -u | wc -l
```

**Users** counts *templates that draw the family*, and every number in the
column is reproduced by one command, the row's block name substituted:

```sh
grep -rlP 'class="[^"]*\bpf-table\b(?!-)' web/templates | wc -l
```

The `(?!-)` matters: without it `pf-empty` matches the legacy `pf-empty-state`
and reads 12 instead of 3. Where a row names several families their counts are
given in the row's order. A number here is a count of **files**, not of sites —
a report that says a family has more users than this column does is almost
certainly counting mentions in comments too.

| Family | Modifiers | Serves | Users |
|---|---|---|---|
| `pf-btn` | `--primary`, `--quiet`, `--danger`, `--icon`, `--sm`, `[disabled]` | §2.3.1 | 25 |
| `pf-table` | `__row`, `__slot`, `__sub`, `__unit`, `__chev`, `__actions`, `__id`, `__creating`, `pf-num`, `th[aria-sort]`, `td.pf-pos` / `.pf-neg` | §2.4.1–§2.4.6 | 2 |
| `pf-panel` | `__head`, `__titles`, `__title`, `__sub`, `__foot`, `__chart` | §2.2.6, §2.3.4 | 4 |
| `pf-menu` | `__list`, `__item`, `__item--danger` | §2.3.5 | 2 |
| `pf-actionbar` | `__hint` | §2.3.4 | 8 |
| `pf-form` | `__main`; `pf-field` `--half` / `--wide` / `--full`; `pf-label`, `pf-hint`, `pf-optional` | §2.5.1 | 5 |
| **A-1** `pf-grid` | — (six tracks; `pf-field`'s spans are counted against it) | §2.5.1 | 10 |
| **A-1** `pf-block__title` | — | §2.5.1 | 11 |
| `pf-input` / `pf-select` / `pf-textarea` | `--num`, `.is-unclear`, `[aria-invalid]` | §2.5.1, §2.5.5 | 10 / 8 / 2 |
| `pf-check` | — | §2.5.1 | 1 |
| **A-1** `pf-seg` | `input`, `label`, `:checked + label` | §2.5.2 | 1 |
| **A-1** `pf-choice` | `.is-selected`, `__figure` | §2.5.1, §2.5.3 | 3 |
| **A-1** `pf-stepper` | `__step`, `__dot`, `__label`, `.is-done`, `.is-active` | §2.5.4 | 1 |
| `pf-rail-sum` | `__title` | §2.5.3 | 4 |
| `pf-sum` | `__formula`, `__total` | §2.5.3 | 3 |
| `pf-leg` | `__type`, `__amount`, `em` | §2.5.7 | 6 |
| `pf-facts` | — (the in-panel fact list; the record's `pf-kv`) | §2.5.7 | 2 |
| **A-1** `pf-context` | `dt`, `dd` | §2.5.7 | 4 |
| **A-1** `pf-lenses` / `pf-lens__title` / `pf-delta` | `__label`, `__before`, `__arrow`, `__after`, `__flag` (+ `--warn`, `--breach`) | §2.2.6 (a slot panel's projection) | 1 |
| `pf-state` | `--draft`, `--unsaved`, `--proposed`, `--approved`, `--booked`, `--reversed`, `--cancelled`, **`--dormant`** | §2.6.1 | 5 |
| `pf-note` | `--info`, `--warn`, `--block`, `__lead`, `__inline`, `__sub` | §2.6.2 | 17 |
| `pf-more` | `[open]` | §2.6.4 | 6 |
| `pf-read` | `b` | §2.5.6 | 7 |
| `pf-sources` | `[hidden]` (load-bearing) | §2.6.7 | 1 |
| `pf-activity` | `__btn`, `__panel`, `pf-spin` | §2.2.8, §2.6.5 | 1 |
| `pf-asof` | `__pair` | §2.6.6 | 0 |
| `pf-figures` / `pf-hero` | — | §2.12.5 | 0 |
| `pf-chart` | `__title`, `__legend`, `__key`, `__plot` | §2.12.2 | 0 |
| `pf-flows` / `pf-flow` | `__name`, `__hint` | §2.2.6 | 1 / 1 |
| `pf-filters` | `__field` | §2.4 | 1 |
| `pf-empty` | `__lead` | §2.9.1 | 3 |
| `pf-skel` | `--plot` | §2.9.3 | 0 |
| `pf-setup` / `pf-step` | `__head`, `__title`, `__count`; `__mark`, `__name`, `__sub`, `__tag`, `__action`, `.is-done` | §2.9.2 | 0 |
| `pf-settings` / `pf-setting` | `__group`, `__title`; `__name`, `__sub`, `__control` | §2.7.4 | 0 |
| `pf-switch` | `input:checked`, `:focus-visible` | §2.7.4 | 0 |
| `pf-tabs` / `pf-tab` | `[aria-selected]` | §2.1.5 | 0 |
| `pf-toolbar` | `__spacer` | §2.4 | 0 |
| `pf-crumb` | `__back`, `__sep`, `__here` | §2.1.6 | 2 |
| `pf-acts` / `pf-act` | `__row`, `__warn`, `__text` | §2.4.8 | 0 |
| `pf-kbd` | — | §2.1.7 | 0 |
| `pf-self` | — | §2.7 | 0 |
| `pf-narrow` | — | §2.2.3 | 0 |
| `pf-scroll-x` | — | §2.11.3 | 0 |
| `pf-sr` | — | §2.11.1 | 0 |

**Two derivations use `color-mix`**, the only computed colours in the file:
`.pf-input:hover` and `.pf-btn--danger`. Both keep a literal out of the sheet
without adding a token that exists only for one state.

**The nine A-1 blocks, and where they came from.** `pf-grid` closed an A0d
transcription gap — `.pf-field`'s `span 2 / 3 / 4` had been taken without the
`repeat(6, …)` container they are counted against, so the spans meant nothing
until it landed. `pf-seg`, `pf-choice`, `pf-context` and `pf-block__title` were
transcribed from `shared.css`; `pf-context` differs from `pf-facts` only in
which way the margin runs. `pf-lenses` / `pf-lens` / `pf-delta` were
transcribed together as the projection family, its one `@container` rule
omitted (§5.4). **`pf-stepper` alone was authored, not transcribed**: the
record names it in prose and lists it *to build*, and its `.pf-step` belongs to
the setup checklist — so the family was written from the specification and its
item named `pf-stepper__step`. `pf-btn--danger` is not new; A-1 gave it its
**first user**.

**Still to build** (in the record, absent here): `pf-finding` (R8), `pf-meter`
(R6), `pf-triplet`, `pf-charts-grid`, `pf-inv`, `pf-kpis`, `pf-ask`,
`pf-fresh`. The record's `pf-nav`, `pf-view`, `pf-search` and `pf-skip` exist
in `layout.css` under the product's own names. The record's `pf-kv` exists
here as **`pf-facts`** — a rename, not a gap.

**`pf-mono` is not on that list, because it is not a family.** The record names
none, and five elements want the mono stack: `#tx-id-value`, `#tx-id-figi`,
`#tx-currency`, `#tx-md-currency` and `#tx-reverse-cause`. They carry it on
their **ids**, in one scoped rule, rather than on an invented class. Two
strings gave their mono up rather than join them. The rule retires the day the
record names the family; inventing it here would have been the larger mistake.

**Scoped exceptions — a shared family in a state the record does not name.**
The Area may scope such a rule, with a comment naming the record question that
retires it; it may **not** add a modifier to the shared family. Registered so
far: `pf-leg`'s type track widened to **124 px** on the two effect-leg surfaces
(the record's 34 px was drawn for `buy` and `sell`, and this Area's vocabulary
runs to `investment_update`); a `pf-choice` that **refuses**; a missing fact on
`pf-context` and its input form on `pf-field`; a `pf-field` holding a control
rather than an input; and the gap between a note and a filter bar. One
constraint is the family's own rather than a scoped rule and is recorded here
because it bites: **`pf-choice__figure` sets `white-space: nowrap`** — right
for the projected balance the record put there, wrong for the sentence a scope
option puts there.

---

## 5. Deviations from the record, accepted

These are decided, not open. Each names why.

1. **The document scrolls.** The record's shell is viewport-high with an
   inner scroll container; the product keeps document scroll and makes both
   columns `position: sticky`. The `intersect once` loaders and
   `tools/ux_atlas.py` both drive the page by scrolling it.
2. **The status bar is sticky at the bottom**, a consequence of 1: without it
   it sits below the fold on any view taller than the viewport.
3. **Navigation labels may wrap.** "Investor Communication" takes two lines
   at 252 px; §2.1.8 is read as covering Area labels too.
4. **No container queries.** `.pf-main` declares no container, so the
   record's twelve `@container view` rules would be inert and are not
   transcribed; the projection family's own `@container` rule was omitted with
   them. When the declaration lands, re-check the sticky view head —
   `container-type: inline-size` can change how `position: sticky` resolves.
   **The first layout-level victim has arrived:** `.pf-lenses` keeps three
   fixed tracks inside a table slot, so the impact panel's three columns of
   figures do not collapse on a narrow view. Everything the deviation cost
   before that was cosmetic. The measurement that still says *wait* is in
   §2.2.4: at 1280 px nothing needs to collapse, and the width that hurts is
   below the minimum the layout answers for and is the **nav auto-collapse's**
   to fix (§2.2.1, strand A-5). Declaring the container, the twelve rules and
   the nav collapse is one piece of work, not three.
5. **Cases scopes the button family.** `components/cases.css` carries
   `.pf-cases .pf-btn` rules that out-specify the global family — strand A-3
   removes them.
6. **`/chat/dock` is loaded lazily**, once per page life, rather than
   rendered into every Area page: inline it would cost two tenant-scoped
   reads on every render of all nine Areas.
7. **The status bar's shortcut hint is retired** in favour of the view head's
   search field (§2.1.7 makes the field the affordance).
8. **`prefers-contrast: more` reaches the default theme only.** The remap
   writes `:root`, which `:root[data-theme="…"]` out-specifies.
9. **`data-a11y` has no writer** — the toggle is strand A-4's. The remap
    block is complete and correct; set the attribute by hand in the console to
    test (§8).
10. **Shirley's state is not persisted** across full page loads; every load
    starts `closed` unless the server says otherwise.
11. **The model line has no home** — §2.10.3 sends it to Providers &
    Credentials; strand A-7.
12. **Twenty `type="number"` are still outside R10.** The parser exists and
    imports nothing (`core/decimal_input.py`); what is missing is the sweep,
    and it is per-Area: investments 7, planning desk 4, SAA 4, watch desk 3,
    portfolio analysis 2. Each one owes the *Read as …* echo too — see §2.5.6.
13. **`login.css:18` is the one colour literal outside `theme.css`**, a
    `box-shadow` with an `rgba()` in it. `_auth_base.html` does not link the
    component vocabulary, so the login card keeps its own; the exemption is
    explicit and budgeted at exactly one, in
    `tests/web/test_css_tokens.py:44`.

---

## 6. Shortcuts

Two, both in `web/static/js/section_nav.js` — one file owns the shell's
keyboard.

| Shortcut | Does | Predicate | Handler | Written where |
|---|---|---|---|---|
| Ctrl K / Cmd K | Opens the command palette | `isPaletteHotkey`, `section_nav.js:180` | `section_nav.js:206` | `areas/_section.html:49–50` — `aria-keyshortcuts="Control+K"` and a visible `<kbd>Ctrl K</kbd>` |
| Ctrl J / Cmd J | Toggles Shirley closed ↔ last open state; **inert on a surface that renders no rail** | `isShirleyHotkey`, `section_nav.js:190` | `section_nav.js:292` → `window.pfShirley.toggle` | `base.html:131` — `title="Open Shirley  Ctrl J"` |

Both are written "Ctrl K" / "Ctrl J" in copy, never "⌘K" or "Ctrl+K". A new
shortcut goes in this file, in this table, and in its control's tooltip.

The Ctrl J guard sits in `shirley.js`'s `toggle()`, not in the key handler, so
it covers every caller of the public API: a surface that overrides
`{% block shirley %}` to nothing has no column to widen, and the shortcut must
do nothing rather than write `data-shirley` onto a shell that cannot answer it.
The predicate is `.pf-side .pf-rail`, not the bare class — a case detail uses
`pf-rail` for an aside of its own.

---

## 7. How to add a view

1. **Catalogue entry.** Add a `SectionMeta(slug=…, title=…)` to the Area's
   tuple in `_SECTIONS_BY_AREA` (`web/shell.py`), in the order it should
   appear. `landing=True` on at most one per Area; `owner_only=True` where
   the route gate refuses a member, so the nav and the command search omit
   it rather than pointing at a body that will not render.
2. **One include.** In `web/templates/_partials/areas/<area>_body.html`:

   ```jinja
   {% with section_slug="my-view",
           section_body_template="_partials/my_view_lazy.html" %}
       {% include "areas/_section.html" %}
   {% endwith %}
   ```

   The heading comes from the catalogue — there is no `section_title`
   parameter, by design.
3. **The loader.** The body partial is a shell with
   `hx-trigger="intersect once"`, `hx-target="this"`, `hx-swap="outerHTML"`
   and a skeleton inside. A hidden section never intersects, so a
   non-landing view costs nothing until it is opened.
4. **Head slots.** `section_as_of` renders the as-of stamp (§2.6.6);
   `section_primary_template` renders the view's one primary action
   (§2.2.2). Both are optional; the action slot collapses when empty.
5. **The regression test pins it.**
   `tests/regression/test_section_catalogue_matches_body_partials.py`
   compares the catalogue against the body partial's `section_slug` values
   and order, and pins `owner_only` to the partial's
   `{% if is_tenant_owner %}` block.
6. **Route tests** in `tests/web/test_<module>_routes.py` pin the section's
   body (`data-pf-section="<slug>"`) and its second-level nav entry
   (`data-pf-section-link="<slug>"`).

### Tree-wide pins

Beyond a view's own tests, these assertions hold across the tree. A change
that breaks one of them is a change to this document, not to a test.

| Pins | Where |
|---|---|
| The component vocabulary's family names exist in the sheet — one parametrised case per name, so a family cannot be referenced from a template it was never declared for | `tests/web/test_pf_components.py:48` (`_EXPECTED_CLASSES`), `:311` |
| **No bespoke class under an Area that has migrated** — every `class="…"` token in `_partials/transactions/*.html`, ids excluded | `tests/web/test_pf_components.py:467` |
| **No bespoke class selector in a migrated Area's sheet**, and every surviving selector `.pf-<area>`-scoped | `tests/web/test_pf_components.py:490` |
| **R1, one primary per view** — six surfaces, each asserted to hold exactly one `pf-btn--primary` | `tests/web/test_transactions_composer.py:1641`, `…_commitment.py:846`, `…_secondary_buy.py:956`, `…_secondary_sale.py:988`, `…_wizard.py:1222` and `:1411` |
| **R10, the number parser** — 41 cases over 4 functions (28 notations, 11 "was it read", 2 properties), all `Decimal`, never `float` | `tests/core/test_decimal_input.py` |
| **R9, the as-of slot** — every section head renders one, whether or not a body fills it | `tests/web/test_transactions_area.py:240` |
| **R4, the stepper is an indicator** — no `hx-`, no `<button>`, no `<a>` between the list's tags | `tests/web/test_transactions_wizard.py:1193` |
| A sub-surface swapped into a hidden section **switches the shell to it** (§1, `shell.js`) | `tests/web/test_transactions_composer.py:1666` |
| One markup selector per shared surface, so a family rename is one edit — e.g. the flow chooser's | `tests/web/conftest.py:217` (`chooser_markup`) |
| No colour literal outside `theme.css`, one budgeted exemption | `tests/web/test_css_tokens.py:44` |

---

## 8. Appendix — the 125 % checklist

Record §2.11.3. Run the browser at **125 % zoom**, 1280 px wide, Shirley
**docked**. Then again with accessible mode on — there is no toggle yet
(§5.9), so set it in the console:

```js
document.querySelector('.pf-shell').dataset.a11y = 'on'
```

### Per Area — nine rows

| Area | Head readable & sticky | 2nd nav level does not clip | Primary action without h-scroll | Tables scroll in `pf-scroll-x`, not the page | Shirley dock usable | Status bar not truncated | Focus ring visible on Tab through the head | Found |
|---|---|---|---|---|---|---|---|---|
| Front Office | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Back Office | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Assistants | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Planning Desk | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Investor Communication | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Watch Desk | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Cases | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Transactions | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |
| Admin | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | |

### Accessible mode — four rows, once

| Check | Expected | ☐ | Found |
|---|---|---|---|
| Font scale stepped up | 14 / 16 / 17 / 19 / 24 px, roughly 120 % | ☐ | |
| Control heights | 44 px (36 px for `--sm`), inputs 44 px | ☐ | |
| Motion off | No skeleton pulse, no spinner, no dock transition | ☐ | |
| Underlines on | Links and `pf-btn--quiet` underlined; nav items **not** | ☐ | |

A "found" entry is a defect for the Area's own strand, not for A-0.

### Three rules the strands have had to learn

Not zoom checks — standing rules, each one a mistake that was made once.

1. **One primary per view means *including* inside a sub-surface.** A panel
   that opens in a table slot, a step of a wizard, a confirmation screen: each
   is part of its view, not a view of its own. Count the accents on the page,
   not in the partial.
2. **A shared family in a state the record does not name is a scoped rule with
   a comment naming its record question — never a new modifier.** Adding
   `--refused` to `pf-choice` puts a decision in the shared layer that the
   record has not made; scoping `.pf-<area> .pf-choice.is-refused` puts it
   where it can be found and removed. Every such rule names what retires it
   (§4).
3. **Two DB-backed `pytest` runs never overlap.** The second truncates the
   schema under the first, and the failures it produces are `303 == 200`
   session losses and foreign-key errors that look like real defects and are
   not. Run selections serially, one process at a time; the full suite gets
   the machine to itself.
